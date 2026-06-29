<?php

declare(strict_types=1);

namespace Vultr\Mcp;

use Dotenv\Dotenv;
use Psr\Http\Message\ResponseInterface;
use Mcp\Server as McpServer;
use Mcp\Server\Transport\StdioTransport;
use Mcp\Server\Transport\StreamableHttpTransport;
use Nyholm\Psr7Server\ServerRequestCreator;
use Nyholm\Psr7\Factory\Psr17Factory;
use Psr\Container\ContainerInterface;
use Vultr\Mcp\Auth\VultrAuth;
use Vultr\Mcp\Http\HealthCheckMiddleware;
use Mcp\Server\Session\Psr16SessionStore;
use Symfony\Component\Cache\Adapter\RedisAdapter;
use Vultr\Mcp\Utils\VultrClientFactory;

$root = dirname(__DIR__);

if (!class_exists(\Composer\Autoload\ClassLoader::class)) {
    require_once $root . '/vendor/autoload.php';
}

if (file_exists($root . '/.env')) {
    Dotenv::createMutable($root)->safeLoad();
}

$transportMode = $_ENV['VULTR_MCP_TRANSPORT'] ?? getenv('VULTR_MCP_TRANSPORT') ?: null;

if ($transportMode === null) {
    if (function_exists('posix_isatty')) {
        $transportMode = posix_isatty(STDIN) ? 'http' : 'stdio';
    } elseif (!defined('STDIN')) {
        $transportMode = 'http';
    } else {
        $transportMode = 'stdio';
    }
}

$transportMode = strtolower($transportMode);

if (!in_array($transportMode, ['stdio', 'http'], true)) {
    fwrite(STDERR, "Error: VULTR_MCP_TRANSPORT must be 'stdio' or 'http', got '{$transportMode}'" . PHP_EOL);
    exit(1);
}

$isStdio = $transportMode === 'stdio';
$isHttp  = $transportMode === 'http';

$perUserMode = filter_var(
    $_ENV['VULTR_PER_USER_MODE'] ?? getenv('VULTR_PER_USER_MODE') ?: 'false',
    FILTER_VALIDATE_BOOLEAN,
);

$defaultApiKey = $_ENV['VULTR_API_KEY'] ?? getenv('VULTR_API_KEY') ?: null;

if ($isStdio) {
    $perUserMode = true;
    if (!empty($defaultApiKey)) {
        \Vultr\Mcp\Utils\RequestContext::setApiKey($defaultApiKey);
    }
} elseif (!$perUserMode && empty($defaultApiKey)) {
    http_response_code(500);
    echo json_encode([
        'error' => 'Server misconfiguration: VULTR_API_KEY is not set. '
                 . 'Set VULTR_PER_USER_MODE=true for per-user API key mode.',
    ]);
    exit(1);
}

// ---------------------------------------------------------------------------
// VultrClientFactory
// ---------------------------------------------------------------------------

$clientFactory = new VultrClientFactory(
    perUserMode: $perUserMode,
    defaultApiKey: $defaultApiKey,
);

// ---------------------------------------------------------------------------
// Tag → Tool class mapping (path-based routing)
// ---------------------------------------------------------------------------
// The request path determines which tools are exposed:
//   /              → all tools (494)
//   /instances     → InstanceTools only (35 tools)
//   /kubernetes    → KubernetesTools only (26 tools)
//   /baremetal     → BareMetalTools only (25 tools)
//   etc.
// In STDIO mode, all tools are always loaded.

$TAG_TO_CLASS = [
    'instances'            => 'InstanceTools',
    'baremetal'            => 'BareMetalTools',
    'kubernetes'           => 'KubernetesTools',
    'load-balancer'        => 'LoadBalancerTools',
    'dns'                  => 'DnsTools',
    'firewall'             => 'FirewallTools',
    'snapshot'             => 'SnapshotTools',
    'ssh'                  => 'SshKeyTools',
    'plans'                => 'PlanTools',
    'region'               => 'RegionTools',
    'os'                   => 'OsTools',
    'vpcs'                 => 'VpcsTools',
    'block'                => 'BlockStorageTools',
    'reserved-ip'          => 'ReservedIpTools',
    'iso'                  => 'IsoTools',
    'account'              => 'AccountTools',
    'api-keys'             => 'ApiKeysTools',
    'application'          => 'ApplicationTools',
    'backup'               => 'BackupTools',
    'billing'              => 'BillingTools',
    'cdns'                 => 'CdnsTools',
    'clusters'             => 'ClustersTools',
    'container-registry'   => 'ContainerRegistryTools',
    'iam'                  => 'IamTools',
    'instance-templates'   => 'InstanceTemplatesTools',
    'logs'                 => 'LogsTools',
    'managed-databases'    => 'ManagedDatabasesTools',
    'marketplace'          => 'MarketplaceTools',
    'oidc'                 => 'OidcTools',
    'organizations'        => 'OrganizationsTools',
    's3'                   => 'S3Tools',
    'scim'                 => 'ScimTools',
    'serverless-inference' => 'ServerlessInferenceTools',
    'startup'              => 'StartupTools',
    'storage-gateways'     => 'StorageGatewaysTools',
    'subaccount'           => 'SubaccountTools',
    'tickets'              => 'TicketsTools',
    'users'                => 'UsersTools',
    'vfs'                  => 'VfsTools',
];

// ---------------------------------------------------------------------------
// Determine which tools to load based on request path (HTTP) or all (STDIO)
// ---------------------------------------------------------------------------

$activeClasses = null; // null = load all

if ($isHttp) {
    // We need the request path to determine the category.
    // The handler closure below will filter tools per-request.
    // For now, load ALL tool classes (they'll be filtered at registration time).
}

$toolsDir = $root . '/src/Tools';
$allToolServices = [];
$allToolRegistrations = [];

// Load all tool classes upfront (cheap — just require + instantiate)
foreach (glob($toolsDir . '/*Tools.php') as $toolFile) {
    require_once $toolFile;
    $className = 'Vultr\Mcp\Tools\\' . basename($toolFile, '.php');
    if (!class_exists($className)) {
        continue;
    }

    $instance = new $className($clientFactory);
    $allToolServices[$className] = $instance;

    // Scan public methods for #[McpTool] attributes
    $reflection = new \ReflectionClass($className);
    foreach ($reflection->getMethods(\ReflectionMethod::IS_PUBLIC) as $method) {
        $attrs = $method->getAttributes(\Mcp\Capability\Attribute\McpTool::class);
        if (empty($attrs)) {
            continue;
        }
        foreach ($attrs as $attr) {
            $toolAttr = $attr->newInstance();
            $toolName = $toolAttr->name ?? $method->getName();
            $allToolRegistrations[] = [
                'class'    => $className,
                'handler'  => [$className, $method->getName()],
                'name'     => $toolName,
            ];
        }
    }
}

// In STDIO mode, register all tools on a single server
if ($isStdio) {
    $toolServices = $allToolServices;
    $toolRegistrations = $allToolRegistrations;

    $container = new class($toolServices) implements ContainerInterface {
        public function __construct(private readonly array $services) {}
        public function get(string $id): mixed
        {
            if (!$this->has($id)) {
                throw new \RuntimeException("Service not found: {$id}");
            }
            return $this->services[$id];
        }
        public function has(string $id): bool
        {
            return isset($this->services[$id]);
        }
    };

    $serverBuilder = McpServer::builder()
        ->setServerInfo('Vultr MCP Server', '1.2.0')
        ->setContainer($container)
        ->setPaginationLimit(1000);

    foreach ($toolRegistrations as $reg) {
        $serverBuilder = $serverBuilder->addTool($reg['handler'], $reg['name']);
    }

    $redisUrl = 'redis://' . ($_ENV['REDIS_HOST'] ?? 'redis') . ':' . (int)($_ENV['REDIS_PORT'] ?? 6379);
    $sessionStore = new Psr16SessionStore(
        cache: new \Symfony\Component\Cache\Psr16Cache(new RedisAdapter(\Symfony\Component\Cache\Adapter\RedisAdapter::createConnection($redisUrl))),
        prefix: 'mcp-session-',
        ttl: 3600,
    );

    $server = $serverBuilder->setSession($sessionStore)->build();

    $transport = new StdioTransport();
    $server->run($transport);
    exit(0);
}

// ---------------------------------------------------------------------------
// HTTP mode — per-request server with path-based tool filtering
// ---------------------------------------------------------------------------

$psr17Factory = new Psr17Factory();
$healthMiddleware = new HealthCheckMiddleware($psr17Factory);
$authMiddleware = VultrAuth::fromEnv($psr17Factory);

$handler = function () use (
    $allToolServices, $allToolRegistrations, $TAG_TO_CLASS,
    $psr17Factory, $healthMiddleware, $authMiddleware
): void {
    $creator = new ServerRequestCreator(
        $psr17Factory, $psr17Factory,
        $psr17Factory, $psr17Factory,
    );

    $request = $creator->fromGlobals();
    $path = $request->getUri()->getPath();
    $path = '/' . ltrim($path, '/');

    // Health check
    if ($request->getMethod() === 'GET' && $path === '/healthz') {
        header('Content-Type: application/json');
        http_response_code(200);
        echo json_encode([
            'status'  => 'ok',
            'service' => 'vultr-mcp-server',
            'version' => '1.2.0',
        ], JSON_THROW_ON_ERROR);
        return;
    }

    // Strip trailing slash
    $path = rtrim($path, '/');
    if ($path === '') {
        $path = '/';
    }

    // Determine which tool classes to expose based on path
    $activeClassNames = null; // null = all

    if ($path === '/' || $path === '/mcp') {
        // Root → all tools (for clients with limited MCP connections)
        $activeClassNames = null;
    } else {
        // Extract category from path: /instances, /kubernetes, etc.
        $category = ltrim($path, '/');
        // Remove /mcp prefix if present (e.g. /mcp/instances → instances)
        if (str_starts_with($category, 'mcp/')) {
            $category = substr($category, 4);
        }

        if (isset($TAG_TO_CLASS[$category])) {
            $activeClassNames = ['Vultr\Mcp\Tools\\' . $TAG_TO_CLASS[$category]];
        } else {
            // Unknown path — 404
            header('Content-Type: application/json');
            http_response_code(404);
            echo json_encode([
                'error' => 'Unknown category',
                'message' => "Unknown path: {$path}. Available categories: " . implode(', ', array_keys($TAG_TO_CLASS)),
            ], JSON_THROW_ON_ERROR);
            return;
        }
    }

    // Filter tools based on active classes
    if ($activeClassNames === null) {
        $toolServices = $allToolServices;
        $toolRegistrations = $allToolRegistrations;
    } else {
        $toolServices = array_filter($allToolServices, function ($key) use ($activeClassNames) {
            return in_array($key, $activeClassNames, true);
        }, ARRAY_FILTER_USE_KEY);

        $toolRegistrations = array_filter($allToolRegistrations, function ($reg) use ($activeClassNames) {
            return in_array($reg['class'], $activeClassNames, true);
        });
    }

    // Build container with filtered services
    $container = new class($toolServices) implements ContainerInterface {
        public function __construct(private readonly array $services) {}
        public function get(string $id): mixed
        {
            if (!$this->has($id)) {
                throw new \RuntimeException("Service not found: {$id}");
            }
            return $this->services[$id];
        }
        public function has(string $id): bool
        {
            return isset($this->services[$id]);
        }
    };

    $serverBuilder = McpServer::builder()
        ->setServerInfo('Vultr MCP Server', '1.2.0')
        ->setContainer($container)
        ->setPaginationLimit(1000);

    foreach ($toolRegistrations as $reg) {
        $serverBuilder = $serverBuilder->addTool($reg['handler'], $reg['name']);
    }

    $redisUrl = 'redis://' . ($_ENV['REDIS_HOST'] ?? 'redis') . ':' . (int)($_ENV['REDIS_PORT'] ?? 6379);
    $sessionStore = new Psr16SessionStore(
        cache: new \Symfony\Component\Cache\Psr16Cache(new RedisAdapter(\Symfony\Component\Cache\Adapter\RedisAdapter::createConnection($redisUrl))),
        prefix: 'mcp-session-',
        ttl: 3600,
    );

    $server = $serverBuilder->setSession($sessionStore)->build();

    $transport = new StreamableHttpTransport(
        request: $request,
        responseFactory: $psr17Factory,
        streamFactory: $psr17Factory,
        middleware: [
            $healthMiddleware,
            $authMiddleware,
        ],
    );

    $response = $server->run($transport);

    (function (ResponseInterface $response) {
        http_response_code($response->getStatusCode());
        foreach ($response->getHeaders() as $name => $values) {
            foreach ($values as $value) {
                header("{$name}: {$value}", false);
            }
        }
        $body = $response->getBody();
        if ($body->isSeekable()) {
            $body->rewind();
        }
        while (!$body->eof()) {
            echo $body->read(8192);
        }
    })($response);

    \Vultr\Mcp\Utils\RequestContext::clear();
};

// FrankenPHP worker mode
if (function_exists('frankenphp_handle_request')) {
    while (true) {
        frankenphp_handle_request($handler);
    }
} else {
    $handler();
}
