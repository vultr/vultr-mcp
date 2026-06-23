<?php

declare(strict_types=1);

namespace Vultr\Mcp;

use Dotenv\Dotenv;
use Laminas\HttpHandlerRunner\Emitter\SapiEmitter;
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
use Vultr\Mcp\Utils\VultrClientFactory;

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

$root = dirname(__DIR__);

require_once $root . '/vendor/autoload.php';

// Load environment variables. .env values take precedence over system env vars.
if (file_exists($root . '/.env')) {
    Dotenv::createMutable($root)->safeLoad();
}

// ---------------------------------------------------------------------------
// Transport mode detection
// ---------------------------------------------------------------------------

// VULTR_MCP_TRANSPORT controls which transport to use:
//   "stdio"  — Local mode: reads from STDIN, writes to STDOUT. For CLI / Claude Desktop / VS Code.
//   "http"   — Remote mode: Streamable HTTP + SSE. For hosted / Kubernetes deployments.
//
// Auto-detection: if STDIN is a TTY (interactive terminal), assume HTTP mode
// since there's no parent process piping JSON-RPC messages. Otherwise default
// to STDIO so that `php src/Server.php` just works when launched by an MCP client.

$transportMode = $_ENV['VULTR_MCP_TRANSPORT'] ?? getenv('VULTR_MCP_TRANSPORT') ?: null;

if ($transportMode === null) {
    // Auto-detect: if stdin is a TTY, we're probably running interactively → HTTP
    $transportMode = (function_exists('posix_isatty') && posix_isatty(STDIN)) ? 'http' : 'stdio';
}

$transportMode = strtolower($transportMode);

if (!in_array($transportMode, ['stdio', 'http'], true)) {
    fwrite(STDERR, "Error: VULTR_MCP_TRANSPORT must be 'stdio' or 'http', got '{$transportMode}'" . PHP_EOL);
    exit(1);
}

$isStdio = $transportMode === 'stdio';
$isHttp  = $transportMode === 'http';

// ---------------------------------------------------------------------------
// Vultr API key mode detection
// ---------------------------------------------------------------------------

$perUserMode = filter_var(
    $_ENV['VULTR_PER_USER_MODE'] ?? getenv('VULTR_PER_USER_MODE') ?: 'false',
    FILTER_VALIDATE_BOOLEAN,
);

// In STDIO mode, per-user is forced on unless a VULTR_API_KEY is explicitly set.
// The rationale: STDIO runs locally, the user's key comes from the environment
// they launched the process in. No X-Vultr-API-Key header is needed.
// In HTTP mode, per-user requires the X-Vultr-API-Key header per request.
$defaultApiKey = $_ENV['VULTR_API_KEY'] ?? getenv('VULTR_API_KEY') ?: null;

if ($isStdio) {
    // STDIO: always use per-user mode. The API key comes from the env
    // the user set when launching the process (e.g. VULTR_API_KEY=*** php src/Server.php).
    $perUserMode = true;
    // If VULTR_API_KEY is set, treat it as the default for this process.
    if (!empty($defaultApiKey)) {
        \Vultr\Mcp\Utils\RequestContext::setApiKey($defaultApiKey);
    }
} elseif (!$perUserMode && empty($defaultApiKey)) {
    // HTTP legacy mode requires a global API key.
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

// ---------------------------------------------------------------------------
// Tool classes
// ---------------------------------------------------------------------------

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

// Stateless: no ->setSession() call. The server operates without
// server-side session persistence, making it safe for Kubernetes
// deployments with multiple replicas and rolling restarts.

$server = $serverBuilder->build();

// ---------------------------------------------------------------------------
// Run with the selected transport
// ---------------------------------------------------------------------------

if ($isStdio) {
    // =====================================================================
    // STDIO Transport — Local mode
    // =====================================================================
    // Reads JSON-RPC from STDIN, writes responses to STDOUT.
    // Used by Claude Desktop, VS Code Copilot, Cursor, and other MCP clients
    // that spawn the server as a child process.
    //
    // Auth: Not needed — STDIO is a local pipe, no network exposure.
    // API key: Read from VULTR_API_KEY env var (set above in RequestContext).
    // =====================================================================

    $transport = new StdioTransport();
    $server->run($transport);

} else {
    // =====================================================================
    // Streamable HTTP Transport — Remote / hosted mode
    // =====================================================================
    // Single HTTP endpoint with POST (JSON-RPC), GET (SSE stream),
    // DELETE (session teardown), and OPTIONS (CORS pre-flight).
    //
    // Auth: VultrAuth middleware extracts per-user API keys.
    // API key: Per-request via X-Vultr-API-Key header.
    // =====================================================================

    $psr17Factory = new Psr17Factory();
    $creator      = new ServerRequestCreator(
        $psr17Factory,
        $psr17Factory,
        $psr17Factory,
        $psr17Factory,
    );

    $request = $creator->fromGlobals();

    // Build the middleware stack:
    //   1. HealthCheckMiddleware — K8s liveness/readiness probes (/healthz)
    //   2. VultrAuth — extracts per-user API keys, validates MCP_AUTH_TOKEN
    //   3. SDK defaults — CORS, DNS rebinding protection, protocol version
    $healthMiddleware = new HealthCheckMiddleware($psr17Factory);
    $authMiddleware   = VultrAuth::fromEnv($psr17Factory);

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

    (new SapiEmitter())->emit($response);

    // Clean up request-scoped context after the response is sent.
    \Vultr\Mcp\Utils\RequestContext::clear();
}
