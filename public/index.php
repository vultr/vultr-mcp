<?php

declare(strict_types=1);

/**
 * FrankenPHP worker entry point.
 *
 * FrankenPHP keeps this worker script booted in memory between requests.
 * Each incoming HTTP request re-runs Server.php's HTTP path:
 *   Request → StreamableHttpTransport (VultrAuth + HealthCheck middleware)
 *           → MCP SDK → Tool execution → Response
 *
 * STDIO mode is unaffected — use `php bin/console mcp:stdio` instead.
 *
 * Note: This does NOT use Symfony Runtime. The MCP SDK handles HTTP
 * protocol directly — no framework bridging needed.
 */

// Force HTTP transport mode (FrankenPHP never has STDIN as a pipe)
putenv('VULTR_MCP_TRANSPORT=http');

// Server.php handles the full lifecycle:
// env loading, auth middleware, transport, response emission, context cleanup.
// FrankenPHP refreshes superglobals between requests so fromGlobals() works.
require_once dirname(__DIR__).'/src/Server.php';
