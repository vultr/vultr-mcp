<?php

declare(strict_types=1);

namespace Vultr\Mcp\Auth;

use Psr\Http\Message\ResponseFactoryInterface;
use Psr\Http\Message\ResponseInterface;
use Psr\Http\Message\ServerRequestInterface;
use Psr\Http\Server\MiddlewareInterface;
use Psr\Http\Server\RequestHandlerInterface;

/**
 * PSR-15 middleware that authenticates MCP client requests using Bearer tokens.
 *
 * ## Authentication modes
 *
 * ### Development / no-auth
 * Set `MCP_AUTH_TOKEN=` (empty) in your .env to disable authentication.
 * Only recommended for local, trusted environments.
 *
 * ### Static bearer token
 * Set `MCP_AUTH_TOKEN=<secret>` in .env. The client must send the header:
 *   Authorization: Bearer <secret>
 *
 * ### OAuth 2.0 / PKCE (production)
 * Replace {@see VultrAuth::validateToken()} with a call to your JWT validation
 * library (e.g. `firebase/php-jwt`, `lcobucci/jwt`). Issue tokens from a
 * dedicated `/oauth/token` endpoint backed by an OAuth 2.0 server.
 * The PKCE flow (RFC 7636) protects public clients from code-interception
 * attacks and does not require a client secret.
 *
 * Standard PKCE flow:
 *  1. Client generates a random `code_verifier` and derives `code_challenge`
 *     = BASE64URL(SHA256(code_verifier)).
 *  2. Client sends `code_challenge` + `code_challenge_method=S256` with the
 *     authorization request to your OAuth server.
 *  3. OAuth server stores the challenge and returns an auth code.
 *  4. Client exchanges the code + `code_verifier` for an access token.
 *  5. Client sends `Authorization: Bearer <access_token>` to this MCP server.
 *  6. This middleware validates the access token (JWT signature / expiry).
 */
final class VultrAuth implements MiddlewareInterface
{
    /**
     * @param ResponseFactoryInterface $responseFactory PSR-17 factory for creating error responses.
     * @param string|null              $expectedToken   Expected bearer token value.
     *                                                  Pass null or empty string to skip authentication.
     */
    public function __construct(
        private readonly ResponseFactoryInterface $responseFactory,
        private readonly ?string $expectedToken = null,
    ) {}

    /**
     * Build a {@see VultrAuth} instance from environment variables.
     *
     * Reads `MCP_AUTH_TOKEN` from the environment.  If not set or empty,
     * authentication is disabled (development mode).
     */
    public static function fromEnv(ResponseFactoryInterface $responseFactory): self
    {
        $token = getenv('MCP_AUTH_TOKEN') ?: null;

        return new self($responseFactory, $token ?: null);
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

        // Authentication is disabled — pass the request through.
        if ($this->expectedToken === null || $this->expectedToken === '') {
            return $handler->handle($request);
        }

        $authHeader = $request->getHeaderLine('Authorization');

        if (!str_starts_with($authHeader, 'Bearer ')) {
            return $this->unauthorised('Missing or malformed Authorization header. Expected: Bearer <token>');
        }

        $providedToken = substr($authHeader, strlen('Bearer '));

        if (!$this->validateToken($providedToken)) {
            return $this->unauthorised('Invalid or expired bearer token.');
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
