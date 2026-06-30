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
 * KEY DELIVERY — pass your Vultr API key as a Bearer token:
 *
 *   Authorization: Bearer YOUR_VULTR_API_KEY
 *
 * Claude Desktop (claude_desktop_config.json):
 *   {
 *     "mcpServers": {
 *       "vultr": {
 *         "url": "https://vultrmcp.com/instances",
 *         "headers": { "Authorization": "Bearer YOUR_VULTR_API_KEY" }
 *       }
 *     }
 *   }
 *
 * Claude Code:
 *   claude mcp add --transport http vultr https://vultrmcp.com/instances \
 *     --header "Authorization: Bearer YOUR_VULTR_API_KEY"
 *
 * Cursor / VS Code (.cursor/mcp.json or .vscode/mcp.json):
 *   {
 *     "mcpServers": {
 *       "vultr": {
 *         "url": "https://vultrmcp.com/instances",
 *         "headers": { "Authorization": "Bearer YOUR_VULTR_API_KEY" }
 *       }
 *     }
 *   }
 *
 * OPTIONAL SERVER GATE — set MCP_AUTH_TOKEN to add a second auth layer that
 * restricts access to the server itself. When set, the Bearer slot is consumed
 * by the gate token; set VULTR_PER_USER_MODE=false and use a shared VULTR_API_KEY.
 *
 * NOTE: Query parameter auth (?api_key=) is not supported. Query parameters
 * appear in server access logs, which would expose users' full-access Vultr
 * API keys to anyone with log access.
 */
final class VultrAuth implements MiddlewareInterface
{
    /**
     * Request attribute key where the extracted Vultr API key is stored.
     */
    public const VULTR_API_KEY_ATTR = 'vultr_api_key';

    /***/
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
        if ($request->getMethod() === 'OPTIONS') {
            return $handler->handle($request);
        }

        $authHeader = $request->getHeaderLine('Authorization');

        if (!str_starts_with($authHeader, 'Bearer ')) {
            return $this->unauthorised(
                'Missing or malformed Authorization header. '
                . 'Expected: Authorization: Bearer YOUR_VULTR_API_KEY — '
                . 'generate a key at https://my.vultr.com/settings/#settingsapi'
            );
        }

        $bearerToken = substr($authHeader, strlen('Bearer '));

        // Optional server gate: if MCP_AUTH_TOKEN is set, validate the bearer
        // token against it before treating it as a Vultr API key.
        if ($this->expectedToken !== null && $this->expectedToken !== '') {
            if (!$this->validateToken($bearerToken)) {
                return $this->unauthorised('Invalid or expired bearer token.');
            }

            // When a server gate token is active the bearer slot is consumed
            // by the gate — the Vultr API key cannot also be in the bearer slot.
            // In this mode VULTR_PER_USER_MODE should be false (single shared key).
            return $handler->handle($request);
        }

        // No server gate — treat the bearer token as the per-user Vultr API key.
        if ($this->perUserMode && empty($bearerToken)) {
            return $this->unauthorised(
                'A Vultr API key is required. '
                . 'Send it as: Authorization: Bearer YOUR_VULTR_API_KEY — '
                . 'generate a key at https://my.vultr.com/settings/#settingsapi'
            );
        }

        if (!empty($bearerToken)) {
            // Store the extracted key in the request-scoped context so
            // VultrClientFactory can access it when creating per-request clients.
            // This bridges the gap between the PSR-15 middleware layer and the
            // MCP SDK's tool invocation layer (which doesn't pass the request).
            RequestContext::setApiKey($bearerToken);
        }

        return $handler->handle($request);
    }

    /**
     * Constant-time token comparison to prevent timing attacks.
     */
    private function validateToken(string $token): bool
    {
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