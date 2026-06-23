<?php

declare(strict_types=1);

namespace Vultr\Mcp\Http;

use Psr\Http\Message\ResponseFactoryInterface;
use Psr\Http\Message\ResponseInterface;
use Psr\Http\Message\ServerRequestInterface;
use Psr\Http\Server\MiddlewareInterface;
use Psr\Http\Server\RequestHandlerInterface;

/**
 * PSR-15 middleware that responds to Kubernetes health check requests.
 *
 * Returns 200 OK for GET /healthz with a small JSON payload.
 * All other requests pass through to the next handler.
 *
 * This must be registered BEFORE the MCP StreamableHttpTransport
 * in the middleware stack so health checks are handled without
 * entering the MCP protocol layer.
 */
final class HealthCheckMiddleware implements MiddlewareInterface
{
    public function __construct(
        private readonly ResponseFactoryInterface $responseFactory,
    ) {}

    public function process(ServerRequestInterface $request, RequestHandlerInterface $handler): ResponseInterface
    {
        $path = $request->getUri()->getPath();

        if ($request->getMethod() === 'GET' && $path === '/healthz') {
            $body = json_encode([
                'status'  => 'ok',
                'service' => 'vultr-mcp-server',
                'version' => '1.2.0',
            ], JSON_THROW_ON_ERROR);

            $response = $this->responseFactory->createResponse(200);
            $response->getBody()->write($body);

            return $response->withHeader('Content-Type', 'application/json');
        }

        return $handler->handle($request);
    }
}
