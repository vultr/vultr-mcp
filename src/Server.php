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
use Vultr\Mcp\Tools\BareMetalTools;
use Vultr\Mcp\Tools\InstanceTools;
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
        // the user set when launching the process (e.g. VULTR_API_KEY=*** php src/Server.php).
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
// VultrClientFactory — creates per-request VultrClient instances
// ---------------------------------------------------------------------------

$clientFactory = new VultrClientFactory(
    perUserMode: $perUserMode,
    defaultApiKey: $defaultApiKey,
);


$instanceTools  = new InstanceTools($clientFactory);
$bareMetalTools = new BareMetalTools($clientFactory);

// ---------------------------------------------------------------------------
// PSR-11 container — supplies pre-wired instances to the SDK's ReferenceHandler
// ---------------------------------------------------------------------------

$container = new class([
    InstanceTools::class  => $instanceTools,
    BareMetalTools::class => $bareMetalTools,
]) implements ContainerInterface {
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

// ---------------------------------------------------------------------------
// MCP Server construction — manual tool registration
// ---------------------------------------------------------------------------

$serverBuilder = McpServer::builder()
    ->setServerInfo('Vultr MCP Server', '1.2.0')
    ->setContainer($container)

    // --- Instance tools ---
    ->addTool([InstanceTools::class, 'listInstances'],          'list_instances')
    ->addTool([InstanceTools::class, 'createInstance'],         'create_instance')
    ->addTool([InstanceTools::class, 'getInstance'],            'get_instance')
    ->addTool([InstanceTools::class, 'updateInstance'],         'update_instance')
    ->addTool([InstanceTools::class, 'deleteInstance'],         'delete_instance')
    ->addTool([InstanceTools::class, 'startInstance'],          'start_instance')
    ->addTool([InstanceTools::class, 'rebootInstance'],         'reboot_instance')
    ->addTool([InstanceTools::class, 'reinstallInstance'],      'reinstall_instance')
    ->addTool([InstanceTools::class, 'haltInstance'],           'halt_instance')
    ->addTool([InstanceTools::class, 'startInstances'],         'start_instances')
    ->addTool([InstanceTools::class, 'rebootInstances'],        'reboot_instances')
    ->addTool([InstanceTools::class, 'haltInstances'],          'halt_instances')
    ->addTool([InstanceTools::class, 'getInstanceBandwidth'],   'get_instance_bandwidth')
    ->addTool([InstanceTools::class, 'getInstanceUpgrades'],    'get_instance_upgrades')
    ->addTool([InstanceTools::class, 'getInstanceUserData'],    'get_instance_user_data')
    ->addTool([InstanceTools::class, 'listInstanceIpv4'],       'list_instance_ipv4')
    ->addTool([InstanceTools::class, 'listInstanceIpv6'],       'list_instance_ipv6')

    // --- Bare Metal tools ---
    ->addTool([BareMetalTools::class, 'listBareMetals'],        'list_bare_metals')
    ->addTool([BareMetalTools::class, 'createBareMetal'],       'create_bare_metal')
    ->addTool([BareMetalTools::class, 'getBareMetal'],          'get_bare_metal')
    ->addTool([BareMetalTools::class, 'updateBareMetal'],       'update_bare_metal')
    ->addTool([BareMetalTools::class, 'deleteBareMetal'],       'delete_bare_metal')
    ->addTool([BareMetalTools::class, 'startBareMetal'],        'start_bare_metal')
    ->addTool([BareMetalTools::class, 'rebootBareMetal'],       'reboot_bare_metal')
    ->addTool([BareMetalTools::class, 'reinstallBareMetal'],    'reinstall_bare_metal')
    ->addTool([BareMetalTools::class, 'haltBareMetal'],         'halt_bare_metal')
    ->addTool([BareMetalTools::class, 'getBareMetalIpv4'],      'get_bare_metal_ipv4')
    ->addTool([BareMetalTools::class, 'getBareMetalIpv6'],      'get_bare_metal_ipv6')
    ->addTool([BareMetalTools::class, 'getBareMetalBandwidth'], 'get_bare_metal_bandwidth')
    ->addTool([BareMetalTools::class, 'getBareMetalUserData'],  'get_bare_metal_user_data')
    ->addTool([BareMetalTools::class, 'getBareMetalUpgrades'],  'get_bare_metal_upgrades');

// Redis-backed session store for multi-replica support
$redisUrl = 'redis://' . ($_ENV['REDIS_HOST'] ?? 'redis') . ':' . (int)($_ENV['REDIS_PORT'] ?? 6379);
$sessionStore = new Psr16SessionStore(
    cache: new \Symfony\Component\Cache\Psr16Cache(new RedisAdapter(\Symfony\Component\Cache\Adapter\RedisAdapter::createConnection($redisUrl))),
    prefix: 'mcp-session-',
    ttl: 3600,
);

$server = $serverBuilder
    ->setSession($sessionStore)
    ->build();


if ($isStdio) {
    // STDIO - local pipe, no auth, API key from env
            //
            // =====================================================================

    $transport = new StdioTransport();
    $server->run($transport);

} else {
    // HTTP - Streamable HTTP + SSE, per-user auth via X-Vultr-API-Key

    $psr17Factory = new Psr17Factory();
    $healthMiddleware = new HealthCheckMiddleware($psr17Factory);
    $authMiddleware = VultrAuth::fromEnv($psr17Factory);

    // Per-request handler — runs for each incoming HTTP request
    $handler = function () use (
        $server, $psr17Factory, $healthMiddleware, $authMiddleware
    ): void {
        $creator = new ServerRequestCreator(
            $psr17Factory, $psr17Factory,
            $psr17Factory, $psr17Factory,
        );

        $request = $creator->fromGlobals();

        // Handle health checks directly — no MCP transport needed
        if ($request->getMethod() === 'GET' && $request->getUri()->getPath() === '/healthz') {
            header('Content-Type: application/json');
            http_response_code(200);
            echo json_encode([
                'status'  => 'ok',
                'service' => 'vultr-mcp-server',
                'version' => '1.2.0',
            ], JSON_THROW_ON_ERROR);
            return;
        }

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

    // FrankenPHP worker mode: handle each request in a loop
    if (function_exists('frankenphp_handle_request')) {
        while (true) {
            frankenphp_handle_request($handler);
        }
    } else {
        // Fallback for non-worker mode (e.g. php -S)
        $handler();
    }
}
