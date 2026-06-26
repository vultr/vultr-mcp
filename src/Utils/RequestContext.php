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

    public static function setApiKey(?string $apiKey): void
    {
        self::$apiKey = $apiKey;
    }

    public static function getApiKey(): ?string
    {
        return self::$apiKey;
    }

    public static function clear(): void
    {
        self::$apiKey = null;
    }
}
