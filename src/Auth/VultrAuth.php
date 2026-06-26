<?php

declare(strict_types=1);

namespace Vultr\Mcp\Auth;

use Psr\Http\Message\ResponseFactoryInterface;
use Psr\Http\Message\ResponseInterface;
use Psr\Http\Message\ServerRequestInterface;
use Psr\Http\Server\MiddlewareInterface;
use Psr\Http\Server\RequestHandlerInterface;
use Vultr\Mcp\Utils\RequestContext;

/**
 * PSR-15 middleware that authenticates MCP client requests and extracts
 * per-user Vultr API keys.
 *
 * ## Authentication modes
 *
 * ### Development / no-auth
 * Set `MCP_AUTH_TOKEN=` (empty) in your .env to disable MCP-level authentication.
 * Only recommended for local, trusted environments.
 *
 * ### Static bearer token
 * Set `MCP_AUTH_TOKEN=<secret>` in .env. The client must send the header:
 *   Authorization: Bearer <secret>
 *
 * ### Per-user Vultr API key (multi-tenant / public hosting)
 * When `VULTR_PER_USER_MODE=true`, each client must supply their own Vultr API
 * key via the `X-Vultr-API-Key` header. This key is stored as a request attribute
 * (`vultr_api_key`) for downstream use by VultrClientFactory.
 *
 * If MCP_AUTH_TOKEN is also set, it is validated first; the Vultr API key is
 * then extracted separately. This allows a deployment where MCP access is gated
 * by a shared bearer token while each user operates on their own Vultr account.
 *
 * ### OAuth 2.0 / PKCE (production)
 * Replace {@see VultrAuth::validateToken()} with a call to your JWT validation
 * library (e.g. `firebase/php-jwt`, `lcobucci/jwt`). Issue tokens from a
 * dedicated `/oauth/token` endpoint backed by an OAuth 2.0 server.
 * The PKCE flow (RFC 7636) protects public clients from code-interception
 * attacks and does not require a client secret.
 */
final class VultrAuth implements MiddlewareInterface
{
    /**
     * Request attribute key where the extracted Vultr API key is stored.
     */
    public const VULTR_API_KEY_ATTR = 'vultr_api_key';

    /**
     * @param ResponseFactoryInterface $responseFactory PSR-17 factory for creating error responses.
     * @param string|null              $expectedToken   Expected bearer token value.
     *                                                  Pass null or empty string to skip authentication.
     * @param bool                     $perUserMode     When true, require X-Vultr-API-Key header.
     */
    public function __construct(
        private readonly ResponseFactoryInterface $responseFactory,
        private readonly ?string $expectedToken = null,
        private readonly bool $perUserMode = false,
    ) {}

    /**
     * Build a {@see VultrAuth} instance from environment variables.
     *
     * Reads `MCP_AUTH_TOKEN` for the optional bearer-token gate.
     * Reads `VULTR_PER_USER_MODE` to enable per-user API key extraction.
     */
    public static function fromEnv(ResponseFactoryInterface $responseFactory): self
    {
        $token      = getenv('MCP_AUTH_TOKEN') ?: null;
        $perUser    = filter_var(
            $_ENV['VULTR_PER_USER_MODE'] ?? getenv('VULTR_PER_USER_MODE') ?: 'false',
            FILTER_VALIDATE_BOOLEAN,
        );

        return new self($responseFactory, $token ?: null, $perUser);
    }

    /**
     * {@inheritdoc}
     *
     * Allows OPTIONS pre-flight requests through without authentication so
     * the HTTP transport's CORS handling can respond correctly.
     */
    public function process(ServerRequestInterface $request, RequestHandlerInterface $handler): ResponseInterface
    {
        // Always allow OPTIONS (CORS pre-flight) through unauthenticated.
        if ($request->getMethod() === 'OPTIONS') {
            return $handler->handle($request);
        }

        // --- Step 1: Validate MCP-level bearer token (if configured) ---
        if ($this->expectedToken !== null && $this->expectedToken !== '') {
            $authHeader = $request->getHeaderLine('Authorization');

            if (!str_starts_with($authHeader, 'Bearer ')) {
                return $this->unauthorised('Missing or malformed Authorization header. Expected: Bearer <token>');
            }

            $providedToken = substr($authHeader, strlen('Bearer '));

            if (!$this->validateToken($providedToken)) {
                return $this->unauthorised('Invalid or expired bearer token.');
            }
        }

        // --- Step 2: Extract per-user Vultr API key ---
        // Priority: header > query param > Bearer token fallback
        $vultrApiKey = $request->getHeaderLine('X-Vultr-API-Key');

        // Fallback 1: query parameter (compatible with MCP clients that only
        // support URL + env config, not custom headers).
        if (empty($vultrApiKey)) {
            $vultrApiKey = $request->getQueryParams()['api_key'] ?? '';
        }

        // Fallback 2: allow the API key to be passed as a Bearer token when
        // there is no separate MCP_AUTH_TOKEN configured (simple single-layer auth).
        if (empty($vultrApiKey) && ($this->expectedToken === null || $this->expectedToken === '')) {
            $authHeader = $request->getHeaderLine('Authorization');
            if (str_starts_with($authHeader, 'Bearer ')) {
                $vultrApiKey = substr($authHeader, strlen('Bearer '));
            }
        }

        if ($this->perUserMode && empty($vultrApiKey)) {
            return $this->unauthorised(
                'X-Vultr-API-Key header is required. Provide your Vultr API key '
                . '(generate one at https://my.vultr.com/settings/#settingsapi).'
            );
        }

        // Store the extracted key in the request-scoped context so
        // VultrClientFactory can access it when creating per-request clients.
        // This bridges the gap between the PSR-15 middleware layer and the
        // MCP SDK's tool invocation layer (which doesn't pass the request).
        if (!empty($vultrApiKey)) {
            RequestContext::setApiKey($vultrApiKey);
        }

        return $handler->handle($request);
    }

    /**
     * Validate the provided bearer token.
     *
     * Override or replace this method to integrate a JWT library for
     * production OAuth 2.0 token validation.
     *
     * @param string $token The raw token value extracted from the Authorization header.
     */
    private function validateToken(string $token): bool
    {
        // Constant-time comparison to prevent timing attacks.
        return hash_equals($this->expectedToken ?? '', $token);
    }

    /**
     * Create a 401 Unauthorized JSON response.
     */
    private function unauthorised(string $message): ResponseInterface
    {
        $body = json_encode(['error' => 'Unauthorized', 'message' => $message], JSON_THROW_ON_ERROR);

        $response = $this->responseFactory->createResponse(401);
        $response->getBody()->write($body);

        return $response
            ->withHeader('Content-Type', 'application/json')
            ->withHeader('WWW-Authenticate', 'Bearer realm="vultr-mcp-server"');
    }
}
