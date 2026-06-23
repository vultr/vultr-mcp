<?php

declare(strict_types=1);

namespace Vultr\Mcp\Utils;

/**
 * Factory for creating per-request VultrClient instances.
 *
 * In per-user (multi-tenant) mode, each MCP request carries the caller's own
 * Vultr API key (extracted by VultrAuth middleware and stored in RequestContext).
 * This factory produces a VultrClient configured with that key, so tool classes
 * never touch a shared global client.
 *
 * In legacy (single-key) mode, the key is read once from VULTR_API_KEY at
 * construction time and reused for every request.
 */
final class VultrClientFactory
{
    private readonly RateLimiter $rateLimiter;
    private readonly bool $sslVerify;
    private readonly string $baseUri;

    /** @var string|null Cached single-key mode API key */
    private readonly ?string $defaultApiKey;

    /**
     * @param bool $perUserMode When true, the API key is read from RequestContext per-request.
     */
    public function __construct(
        private readonly bool $perUserMode = false,
        ?string $defaultApiKey = null,
    ) {
        $this->rateLimiter = new RateLimiter();
        $this->sslVerify   = filter_var(
            $_ENV['SSL_VERIFY'] ?? getenv('SSL_VERIFY') ?: 'true',
            FILTER_VALIDATE_BOOLEAN,
        );
        $this->baseUri     = ($_ENV['VULTR_API_BASE_URL'] ?? getenv('VULTR_API_BASE_URL')) ?: 'https://api.vultr.com/v2/';
        $this->defaultApiKey = $defaultApiKey;
    }

    /**
     * Create a VultrClient for the current request.
     *
     * In per-user mode, the API key is read from RequestContext (populated
     * by VultrAuth middleware from the X-Vultr-API-Key header).
     * In legacy mode, the constructor-provided default key is used.
     *
     * @return VultrClient
     * @throws \InvalidArgumentException If no valid API key is available.
     */
    public function create(): VultrClient
    {
        $apiKey = $this->perUserMode
            ? RequestContext::getApiKey()
            : $this->defaultApiKey;

        if (empty($apiKey) || $apiKey === 'your_vultr_api_key_here') {
            throw new \InvalidArgumentException(
                'A valid Vultr API key is required. '
                . ($this->perUserMode
                    ? 'Provide it via the X-Vultr-API-Key header.'
                    : 'Set VULTR_API_KEY in your environment or .env file.')
            );
        }

        return new VultrClient(
            apiKey: $apiKey,
            rateLimiter: $this->rateLimiter,
            sslVerify: $this->sslVerify,
            baseUri: $this->baseUri,
        );
    }

    /**
     * Whether this factory operates in per-user mode.
     */
    public function isPerUserMode(): bool
    {
        return $this->perUserMode;
    }
}
