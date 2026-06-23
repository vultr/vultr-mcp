<?php

declare(strict_types=1);

namespace Vultr\Mcp\Utils;

/**
 * Request-scoped holder for the current user's Vultr API key.
 *
 * VultrAuth middleware populates this during request processing,
 * and VultrClientFactory reads from it when creating per-request clients.
 *
 * This is necessary because the MCP SDK invokes tool methods
 * without direct access to the PSR-7 request object.
 *
 * Thread-safety note: PHP runs each request in an isolated process
 * (or coroutine in Swoole/FrankenPHP), so a simple static holder
 * is safe for typical PHP-FPM or built-in server deployments.
 * For true async runtimes, use a fiber-local or coroutine-local store.
 */
final class RequestContext
{
    private static ?string $apiKey = null;

    /**
     * Set the Vultr API key for the current request.
     */
    public static function setApiKey(?string $apiKey): void
    {
        self::$apiKey = $apiKey;
    }

    /**
     * Get the Vultr API key for the current request.
     */
    public static function getApiKey(): ?string
    {
        return self::$apiKey;
    }

    /**
     * Clear the request context (call at the end of request processing).
     */
    public static function clear(): void
    {
        self::$apiKey = null;
    }
}
