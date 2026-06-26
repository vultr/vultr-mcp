<?php

declare(strict_types=1);

namespace Vultr\Mcp\Utils;

/**
 * Request-scoped holder for the current user's Vultr API key.
 * Populated by VultrAuth, read by VultrClientFactory.
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
