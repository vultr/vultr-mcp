<?php

declare(strict_types=1);

namespace Vultr\Mcp\Generator;

/**
 * Generates MCP tool classes from a Vultr OpenAPI v3 specification.
 *
 * ## Usage
 *
 *   $generator = new OpenApiGenerator('/path/to/openapi.json');
 *   $generator->generate(['instances', 'baremetal'], '/path/to/output/dir');
 *
 * Or from the CLI via bin/generate.php:
 *
 *   php bin/generate.php --spec=/path/to/openapi.json --tags=instances,baremetal
 *
 * ## What it does
 *
 * 1. Parses the Vultr OpenAPI JSON.
 * 2. Filters paths by the requested tags (e.g. "instances", "baremetal").
 * 3. For each operation builds a PHP method with:
 *    - Typed parameters inferred from OpenAPI parameter/body schemas.
 *    - A docblock derived from the operation summary and parameter descriptions.
 *    - A VultrClient call using the correct HTTP method and URI template.
 * 4. Groups methods by tag and writes one PHP class file per tag.
 *
 * Generated files are meant to be checked in and committed.  Re-running the
 * generator after a spec update will overwrite them.
 */
final class OpenApiGenerator
{
    /** @var array<string, mixed> Parsed OpenAPI document. */
    private array $spec;

    /** Maps OpenAPI JSON Schema types to PHP type hints. */
    private const TYPE_MAP = [
        'string'  => 'string',
        'integer' => 'int',
        'number'  => 'float',
        'boolean' => 'bool',
        'array'   => 'array',
        'object'  => 'array',
    ];

    /** Maps tag names to PHP class names. */
    private const CLASS_NAMES = [
        'account'              => 'AccountTools',
        'api-keys'             => 'ApiKeysTools',
        'application'          => 'ApplicationTools',
        'backup'               => 'BackupTools',
        'baremetal'             => 'BareMetalTools',
        'billing'              => 'BillingTools',
        'block'                => 'BlockStorageTools',
        'cdns'                 => 'CdnsTools',
        'clusters'             => 'ClustersTools',
        'container registry'   => 'ContainerRegistryTools',
        'dns'                  => 'DnsTools',
        'firewall'             => 'FirewallTools',
        'iam'                  => 'IamTools',
        'instance-templates'   => 'InstanceTemplatesTools',
        'instances'            => 'InstanceTools',
        'iso'                  => 'IsoTools',
        'kubernetes'           => 'KubernetesTools',
        'load-balancer'        => 'LoadBalancerTools',
        'logs'                 => 'LogsTools',
        'managed-databases'    => 'ManagedDatabasesTools',
        'marketplace'          => 'MarketplaceTools',
        'oidc'                 => 'OidcTools',
        'organizations'        => 'OrganizationsTools',
        'os'                   => 'OsTools',
        'plans'                => 'PlanTools',
        'region'               => 'RegionTools',
        'reserved-ip'          => 'ReservedIpTools',
        's3'                   => 'S3Tools',
        'scim'                 => 'ScimTools',
        'serverless-inference' => 'ServerlessInferenceTools',
        'snapshot'             => 'SnapshotTools',
        'ssh'                  => 'SshKeyTools',
        'startup'              => 'StartupTools',
        'storage-gateways'     => 'StorageGatewaysTools',
        'subaccount'           => 'SubaccountTools',
        'tickets'              => 'TicketsTools',
        'users'                => 'UsersTools',
        'vfs'                  => 'VfsTools',
        'vpcs'                 => 'VpcsTools',
    ];

    /** Maps tag names to Vultr API path prefixes. */
    private const PATH_PREFIXES = [
        'account'              => '/account',
        'api-keys'             => '/apikeys',
        'application'          => '/applications',
        'backup'               => '/backups',
        'baremetal'             => '/bare-metals',
        'billing'              => '/billing',
        'block'                => '/blocks',
        'cdns'                 => '/cdns',
        'clusters'             => '/clusters',
        'container registry'   => '/registry',
        'dns'                  => '/domains',
        'firewall'             => '/firewalls',
        'iam'                  => '/v2',
        'instance-templates'   => '/instances/templates',
        'instances'            => '/instances',
        'iso'                  => '/iso',
        'kubernetes'           => '/kubernetes',
        'load-balancer'        => '/load-balancers',
        'logs'                 => '/logs',
        'managed-databases'    => '/databases',
        'marketplace'          => '/marketplace',
        'oidc'                 => '/v2/oidc',
        'organizations'        => '/v2',
        'os'                   => '/os',
        'plans'                => '/plans',
        'region'               => '/regions',
        'reserved-ip'          => '/reserved-ips',
        's3'                   => '/object-storage',
        'scim'                 => '/scim',
        'serverless-inference' => '/inference',
        'snapshot'             => '/snapshots',
        'ssh'                  => '/ssh-keys',
        'startup'              => '/startup-scripts',
        'storage-gateways'     => '/storage-gateways',
        'subaccount'           => '/subaccounts',
        'tickets'              => '/tickets',
        'users'                => '/users',
        'vfs'                  => '/vfs',
        'vpcs'                 => '/vpcs',
    ];

    public function __construct(private readonly string $specPath)
    {
        if (!file_exists($specPath)) {
            throw new \InvalidArgumentException("OpenAPI spec file not found: {$specPath}");
        }

        $raw = file_get_contents($specPath);
        if ($raw === false) {
            throw new \RuntimeException("Unable to read spec file: {$specPath}");
        }

        $this->spec = json_decode($raw, true, 512, JSON_THROW_ON_ERROR);

        if (!isset($this->spec['paths'])) {
            throw new \RuntimeException('Invalid OpenAPI spec: missing "paths" key.');
        }
    }

    /**
     * Generate PHP tool class files for the specified tags.
     *
     * @param  string[] $tags      Tags to include (e.g. ['instances', 'baremetal']).
     * @param  string   $outputDir Directory to write generated files into.
     * @throws \RuntimeException   When a tag is unsupported or output cannot be written.
     */
    public function generate(array $tags, string $outputDir): void
    {
        if (!is_dir($outputDir) && !mkdir($outputDir, 0755, true) && !is_dir($outputDir)) {
            throw new \RuntimeException("Cannot create output directory: {$outputDir}");
        }

        foreach ($tags as $tag) {
            $tag = strtolower($tag);

            if (!isset(self::CLASS_NAMES[$tag])) {
                $supported = implode(', ', array_keys(self::CLASS_NAMES));
                throw new \InvalidArgumentException(
                    "Unsupported tag '{$tag}'. Supported: {$supported}"
                );
            }

            $operations = $this->collectOperations($tag);
            $classCode  = $this->renderClass($tag, $operations);
            $className  = self::CLASS_NAMES[$tag];
            $filePath   = rtrim($outputDir, DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR . "{$className}.php";

            if (file_put_contents($filePath, $classCode) === false) {
                throw new \RuntimeException("Failed to write generated file: {$filePath}");
            }

            echo "Generated {$className} → {$filePath}" . PHP_EOL;
        }
    }

    /**
     * Collect all API operations for a given tag, sorted by path then method.
     *
     * @return list<array{
     *     operationId: string,
     *     summary: string,
     *     description: string,
     *     method: string,
     *     path: string,
     *     pathParams: list<array{name: string, type: string, description: string, required: bool}>,
     *     queryParams: list<array{name: string, type: string, description: string, required: bool}>,
     *     bodyProps: list<array{name: string, type: string, description: string, required: bool, deprecated: bool}>,
     *     requiredBody: list<string>,
     * }>
     */
    private function collectOperations(string $tag): array
    {
        $prefix     = self::PATH_PREFIXES[$tag];
        $operations = [];

        foreach ($this->spec['paths'] as $path => $pathItem) {
            if (!str_starts_with($path, $prefix)) {
                continue;
            }

            // Path-level shared parameters (e.g. the {instance-id} parameter block).
            $sharedParams = $pathItem['parameters'] ?? [];

            foreach (['get', 'post', 'patch', 'put', 'delete'] as $method) {
                if (!isset($pathItem[$method])) {
                    continue;
                }

                $op = $pathItem[$method];

                                if (!in_array($tag, array_map('strtolower', $op['tags'] ?? []), true)) {
                    continue;
                }

                                if ($op['deprecated'] ?? false) {
                    continue;
                }

                $allParams   = array_merge($sharedParams, $op['parameters'] ?? []);
                $pathParams  = $this->extractParams($allParams, 'path');
                $queryParams = $this->extractParams($allParams, 'query');
                $bodyProps   = $this->extractBodyProps($op);
                $required    = $this->extractRequiredBody($op);

                $operations[] = [
                    'operationId'  => $op['operationId'] ?? $this->deriveOperationId($method, $path),
                    'summary'      => $op['summary'] ?? '',
                    'description'  => $this->stripMarkdown($op['description'] ?? ''),
                    'method'       => strtoupper($method),
                    'path'         => $path,
                    'pathParams'   => $pathParams,
                    'queryParams'  => $queryParams,
                    'bodyProps'    => $bodyProps,
                    'requiredBody' => $required,
                ];
            }
        }

        usort($operations, static fn($a, $b) => [$a['path'], $a['method']] <=> [$b['path'], $b['method']]);

        return $operations;
    }

    /**
     * Extract typed parameter definitions from an OpenAPI parameters array.
     *
     * @param  list<array<string, mixed>> $params
     * @return list<array{name: string, type: string, description: string, required: bool}>
     */
    private function extractParams(array $params, string $in): array
    {
        $result = [];

        foreach ($params as $param) {
            if (($param['in'] ?? '') !== $in) {
                continue;
            }

            if ($param['deprecated'] ?? false) {
                continue;
            }

            $schema = $param['schema'] ?? [];

            $result[] = [
                'name'        => $param['name'],
                'type'        => self::TYPE_MAP[$schema['type'] ?? 'string'] ?? 'string',
                'description' => $this->stripMarkdown($param['description'] ?? ''),
                'required'    => (bool) ($param['required'] ?? false),
            ];
        }

        return $result;
    }

    /**
     * Extract request body property definitions from an OpenAPI operation.
     *
     * @return list<array{name: string, type: string, description: string, required: bool, deprecated: bool}>
     */
    private function extractBodyProps(array $op): array
    {
        $schema     = $op['requestBody']['content']['application/json']['schema'] ?? null;
        $required   = $schema['required'] ?? [];
        $properties = $schema['properties'] ?? [];
        $result     = [];

        foreach ($properties as $name => $prop) {
            if ($prop['deprecated'] ?? false) {
                continue;
            }

            $type = self::TYPE_MAP[$prop['type'] ?? 'string'] ?? 'string';

            $result[] = [
                'name'        => $name,
                'type'        => $type,
                'description' => $this->stripMarkdown($prop['description'] ?? ''),
                'required'    => in_array($name, $required, true),
                'deprecated'  => false,
            ];
        }

        return $result;
    }

    /**
     * Extract the list of required body property names.
     *
     * @return list<string>
     */
    private function extractRequiredBody(array $op): array
    {
        return $op['requestBody']['content']['application/json']['schema']['required'] ?? [];
    }

    /**
     * Render a complete PHP class file for a tag.
     */
    private function renderClass(string $tag, array $operations): string
    {
        $className = self::CLASS_NAMES[$tag];
        $prefix    = self::PATH_PREFIXES[$tag];
        $methods   = '';
        $seenNames = [];

        foreach ($operations as $op) {
            $op = $this->deduplicateOperationId($op, $seenNames);
            $methods .= $this->renderMethod($op) . "\n";
        }

        return <<<PHP
<?php

declare(strict_types=1);

namespace Vultr\\Mcp\\Tools;

use Mcp\\Capability\\Attribute\\McpTool;
use Mcp\\Exception\\ToolCallException;
use Vultr\\Mcp\\Utils\\VultrClient;
use Vultr\\Mcp\\Utils\\VultrClientFactory;

/**
 * MCP tools for Vultr {$prefix} endpoints.
 *
 * AUTO-GENERATED by {@see \\Vultr\\Mcp\\Generator\\OpenApiGenerator}.
 * Re-run `php bin/generate.php` to regenerate from the latest OpenAPI spec.
 *
 * Do not edit this file manually — changes will be overwritten.
 */
final class {$className}
{
    /**
     * @param VultrClientFactory \$clientFactory Factory for per-request VultrClient instances.
     */
    public function __construct(
        private readonly VultrClientFactory \$clientFactory,
    ) {}

    /**
     * Resolve the VultrClient for the current request.
     */
    private function getClient(): VultrClient
    {
        return \$this->clientFactory->create();
    }

{$methods}}
PHP;
    }

    /**
     * Ensure unique operationId + method names within a class.
     *
     * The Vultr OpenAPI spec occasionally reuses an operationId across
     * different endpoints (e.g. get-pushzone for both the zone and a file
     * inside it).  Append a suffix derived from the path so every generated
     * method has a unique name.
     */
    private function deduplicateOperationId(array $op, array &$seenNames): array
    {
        $baseId = $op['operationId'];
        $suffix = 1;

        while (in_array($op['operationId'], $seenNames, true)) {
            // Build a meaningful suffix from the last non-variable path segment
            $pathParts = explode('/', trim($op['path'], '/'));
            $tail      = end($pathParts);
            $tail      = preg_replace('/[^a-zA-Z0-9]/', '', $tail);
            if ($tail !== '' && !str_starts_with($tail, '{')) {
                $op['operationId'] = $baseId . '-' . $tail . ($suffix > 1 ? '-' . $suffix : '');
            } else {
                $op['operationId'] = $baseId . '-' . $suffix;
            }
            $suffix++;
        }

        $seenNames[] = $op['operationId'];
        return $op;
    }

    /**
     * Render a single PHP method for an API operation.
     */
    private function renderMethod(array $op): string
    {
        $methodName = $this->operationIdToMethodName($op['operationId']);
        $toolName   = str_replace('-', '_', $op['operationId']);
        $docLines   = $this->buildDocLines($op);
        $params     = $this->buildParamList($op);
        $body       = $this->buildMethodBody($op);
        $docblock   = $this->renderDocblock($docLines);

        $paramStr = '';
        if (!empty($params)) {
            $lines    = array_map(static fn($p) => "        {$p},", $params);
            $paramStr = "\n" . implode("\n", $lines) . "\n    ";
        }

        return <<<PHP
    /**
{$docblock}
     */
    #[McpTool(name: '{$toolName}')]
    public function {$methodName}({$paramStr}): array
    {
{$body}
    }

PHP;
    }

    /**
     * Build docblock lines (description + @param tags).
     *
     * @return list<string>
     */
    private function buildDocLines(array $op): array
    {
        $lines = [];

        if ($op['summary']) {
            $lines[] = $op['summary'] . '.';
        }

        if ($op['description'] && $op['description'] !== $op['summary'] . '.') {
            $lines[] = '';
            foreach (explode("\n", wordwrap($op['description'], 100)) as $line) {
                $lines[] = $line;
            }
        }

        $hasParams = !empty($op['pathParams']) || !empty($op['queryParams']) || !empty($op['bodyProps']);

        if ($hasParams) {
            $lines[] = '';
        }

        foreach ($op['pathParams'] as $p) {
            $desc    = $p['description'] ? "  {$p['description']}" : '';
            $lines[] = "@param  {$p['type']}  \${$this->camelCase($p['name'])}{$desc}";
        }

        foreach ($op['bodyProps'] as $p) {
            $nullable = $p['required'] ? '' : '|null';
            $phpType  = $p['type'] . $nullable;
            $desc     = $p['description'] ? "  {$p['description']}" : '';
            $lines[]  = "@param  {$phpType}  \${$this->camelCase($p['name'])}{$desc}";
        }

        foreach ($op['queryParams'] as $p) {
            $nullable = $p['required'] ? '' : '|null';
            $phpType  = $p['type'] . $nullable;
            $desc     = $p['description'] ? "  {$p['description']}" : '';
            $lines[]  = "@param  {$phpType}  \${$this->camelCase($p['name'])}{$desc}";
        }

        $lines[] = '@return array<string, mixed>';

        return $lines;
    }

    /**
     * Build the PHP parameter list for a method signature.
     *
     * @return list<string>
     */
    private function buildParamList(array $op): array
    {
        $params = [];

        // Path params are always required and come first.
        foreach ($op['pathParams'] as $p) {
            $params[] = "{$p['type']} \${$this->camelCase($p['name'])}";
        }

        // Required body params come next.
        foreach ($op['bodyProps'] as $p) {
            if ($p['required']) {
                $params[] = "{$p['type']} \${$this->camelCase($p['name'])}";
            }
        }

                foreach ($op['bodyProps'] as $p) {
            if (!$p['required']) {
                $params[] = "?{$p['type']} \${$this->camelCase($p['name'])} = null";
            }
        }

                foreach ($op['queryParams'] as $p) {
            if ($p['required']) {
                $params[] = "{$p['type']} \${$this->camelCase($p['name'])}";
            } else {
                $params[] = "?{$p['type']} \${$this->camelCase($p['name'])} = null";
            }
        }

        return $params;
    }

    /**
     * Build the method body — the VultrClient call.
     */
    private function buildMethodBody(array $op): string
    {
        $pathExpr = $this->buildPathExpression($op['path'], $op['pathParams']);

        switch ($op['method']) {
            case 'GET':
                return $this->buildGetBody($pathExpr, $op['queryParams']);

            case 'DELETE':
                return $this->buildDeleteBody($pathExpr);

            case 'POST':
            case 'PATCH':
            case 'PUT':
                return $this->buildWriteBody($op['method'], $pathExpr, $op['bodyProps']);

            default:
                return "        throw new \\LogicException('Unsupported HTTP method: {$op['method']}');";
        }
    }

    private function buildGetBody(string $pathExpr, array $queryParams): string
    {
        if (empty($queryParams)) {
            return "        return \$this->getClient()->get({$pathExpr});";
        }

        $pairs = [];
        foreach ($queryParams as $p) {
            $varName = $this->camelCase($p['name']);
            $pairs[] = "            '{$p['name']}' => \${$varName},";
        }

        $inner = implode("\n", $pairs);

        return <<<PHP
        return \$this->getClient()->get({$pathExpr}, [
{$inner}
        ]);
PHP;
    }

    private function buildDeleteBody(string $pathExpr): string
    {
        return <<<PHP
        \$this->getClient()->delete({$pathExpr});

        return ['success' => true];
PHP;
    }

    private function buildWriteBody(string $method, string $pathExpr, array $bodyProps): string
    {
        $phpMethod = strtolower($method);

        if (empty($bodyProps)) {
            return "        return \$this->client->{$phpMethod}({$pathExpr});";
        }

        $pairs = [];
        foreach ($bodyProps as $p) {
            $varName = $this->camelCase($p['name']);
            $pairs[] = "            '{$p['name']}' => \${$varName},";
        }

        $inner = implode("\n", $pairs);

        return <<<PHP
        return \$this->client->{$phpMethod}({$pathExpr}, [
{$inner}
        ]);
PHP;
    }

    /**
     * Convert an OpenAPI path template to a PHP string expression.
     *
     * Example: "/instances/{instance-id}/start" → '"/instances/{$instanceId}/start"'
     */
    private function buildPathExpression(string $path, array $pathParams): string
    {
        $expr = $path;

        foreach ($pathParams as $p) {
            $varName = $this->camelCase($p['name']);
            $expr    = str_replace('{' . $p['name'] . '}', "{{$varName}", $expr);
        }

        // If there are any interpolated variables, use a double-quoted string.
        if (str_contains($expr, '{$')) {
            return '"' . str_replace('"', '\\"', $expr) . '"';
        }

        return "'{$expr}'";
    }

    /**
     * Render a docblock body from a list of lines.
     */
    private function renderDocblock(array $lines): string
    {
        return implode("\n", array_map(
            static fn(string $line): string => '     * ' . $line,
            $lines,
        ));
    }

    /**
     * Convert a kebab-case or snake_case operationId to a camelCase PHP method name.
     *
     * Examples:
     *   "list-instances"    → "listInstances"
     *   "halt_baremetal"    → "haltBaremetal"
     *   "get-baremetal"     → "getBaremetal"
     */
    private function operationIdToMethodName(string $operationId): string
    {
        $parts = preg_split('/[-_]/', $operationId);

        return lcfirst(implode('', array_map('ucfirst', $parts)));
    }

    /**
     * Convert a snake_case or kebab-case parameter name to camelCase.
     *
     * Examples:
     *   "per_page"          → "perPage"
     *   "firewall-group-id" → "firewallGroupId"
     *   "instance-id"       → "instanceId"
     */
    private function camelCase(string $name): string
    {
        $parts = preg_split('/[-_]/', $name);

        return lcfirst(implode('', array_map('ucfirst', $parts)));
    }

    /**
     * Derive a fallback operationId from HTTP method and path.
     */
    private function deriveOperationId(string $method, string $path): string
    {
        $segments = array_filter(explode('/', $path));
        $slug     = implode('-', $segments);

        return strtolower($method) . '-' . preg_replace('/[{}]/', '', $slug);
    }

    /**
     * Strip basic Markdown from description text (links, bold, inline code).
     */
    private function stripMarkdown(string $text): string
    {
        // Remove link references [text](#anchor)
        $text = preg_replace('/\[([^\]]+)\]\([^)]*\)/', '$1', $text);
        // Remove bold/italic **text** or *text*
        $text = preg_replace('/\*{1,2}([^*]+)\*{1,2}/', '$1', $text);
        // Remove inline code `code`
        $text = preg_replace('/`([^`]+)`/', '$1', $text);
        // Collapse multiple blank lines
        $text = preg_replace('/\n{3,}/', "\n\n", $text);

        return trim($text);
    }
}
