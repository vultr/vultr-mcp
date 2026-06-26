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
 * per-user Vultr API keys. Supports three modes:
 *
 *   - No auth: empty MCP_AUTH_TOKEN (local/trusted environments only)
 *   - Bearer token: set MCP_AUTH_TOKEN, clients send Authorization: Bearer <token>
 *   - Per-user API key: VULTR_PER_USER_MODE=true, clients send X-Vultr-API-Key
 *
 * Bearer token and per-user API key can be combined: the token gates MCP access
 * while each user operates on their own Vultr account.
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
     * Constant-time token comparison to prevent timing attacks.
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
