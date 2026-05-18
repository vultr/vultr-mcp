<?php

declare(strict_types=1);

namespace Vultr\Mcp;

use Dotenv\Dotenv;
use Laminas\HttpHandlerRunner\Emitter\SapiEmitter;
use Mcp\Server as McpServer;
use Mcp\Server\Session\FileSessionStore;
use Mcp\Server\Transport\StreamableHttpTransport;
use Nyholm\Psr7Server\ServerRequestCreator;
use Nyholm\Psr7\Factory\Psr17Factory;
use Psr\Container\ContainerInterface;
use Vultr\Mcp\Auth\VultrAuth;
use Vultr\Mcp\Tools\BareMetalTools;
use Vultr\Mcp\Tools\InstanceTools;
use Vultr\Mcp\Utils\RateLimiter;
use Vultr\Mcp\Utils\VultrClient;

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
// Configuration validation
// ---------------------------------------------------------------------------

$apiKey = $_ENV['VULTR_API_KEY'] ?? getenv('VULTR_API_KEY');

if (empty($apiKey)) {
    http_response_code(500);
    echo json_encode(['error' => 'Server misconfiguration: VULTR_API_KEY is not set.']);
    exit(1);
}

$sessionsDir = $root . '/sessions';
if (!is_dir($sessionsDir) && !mkdir($sessionsDir, 0750, true) && !is_dir($sessionsDir)) {
    http_response_code(500);
    echo json_encode(['error' => "Unable to create sessions directory: {$sessionsDir}"]);
    exit(1);
}

// ---------------------------------------------------------------------------
// PSR-7 / PSR-17 setup (Nyholm)
// ---------------------------------------------------------------------------

$psr17Factory = new Psr17Factory();
$creator      = new ServerRequestCreator(
    $psr17Factory, // ServerRequestFactory
    $psr17Factory, // UriFactory
    $psr17Factory, // UploadedFileFactory
    $psr17Factory, // StreamFactory
);

$request = $creator->fromGlobals();

// ---------------------------------------------------------------------------
// Tool classes — inject VultrClient
// ---------------------------------------------------------------------------

$sslVerify  = filter_var($_ENV['SSL_VERIFY'] ?? getenv('SSL_VERIFY') ?? 'true', FILTER_VALIDATE_BOOLEAN);
$baseUri    = $_ENV['VULTR_API_BASE_URL'] ?? getenv('VULTR_API_BASE_URL') ?: 'https://api.vultr.com/v2/';
$vultrClient = new VultrClient(apiKey: $apiKey, rateLimiter: new RateLimiter(), sslVerify: $sslVerify, baseUri: $baseUri);
$instanceTools = new InstanceTools(client: $vultrClient);
$bareMetalTools = new BareMetalTools(client: $vultrClient);

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
    ->setServerInfo('Vultr MCP Server', '1.0.0')
    ->setSession(new FileSessionStore($sessionsDir))
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

$server = $serverBuilder->build();

// ---------------------------------------------------------------------------
// Auth middleware (PSR-15)
// ---------------------------------------------------------------------------

$authMiddleware = VultrAuth::fromEnv($psr17Factory);

// ---------------------------------------------------------------------------
// HTTP transport — Streamable HTTP / SSE
// ---------------------------------------------------------------------------

$transport = new StreamableHttpTransport(
    request: $request,
    responseFactory: $psr17Factory,
    streamFactory: $psr17Factory,
    corsHeaders: [],          // Inherit secure defaults from the transport
    middleware: [$authMiddleware],
);

$response = $server->run($transport);

// ---------------------------------------------------------------------------
// Emit PSR-7 response
// ---------------------------------------------------------------------------

(new SapiEmitter())->emit($response);
