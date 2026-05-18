<?php

declare(strict_types=1);

namespace Vultr\Mcp\Utils;

use GuzzleHttp\Client;
use GuzzleHttp\Exception\ClientException;
use GuzzleHttp\Exception\ConnectException;
use GuzzleHttp\Exception\ServerException;
use Mcp\Exception\ToolCallException;

/**
 * Thin Guzzle wrapper for the Vultr REST API v2.
 *
 * Handles authentication (Authorization: Bearer), JSON serialisation,
 * null-value filtering, and delegates rate-limit retries to {@see RateLimiter}.
 *
 * Usage:
 *   $client = new VultrClient(apiKey: 'YOUR_API_KEY');
 *   $data   = $client->get('/instances', ['region' => 'ewr']);
 */
final class VultrClient
{
    private const DEFAULT_BASE_URI = 'https://api.vultr.com/v2/';
    private const TIMEOUT          = 30;

    private readonly Client $http;

    public function __construct(
        private readonly string $apiKey,
        private readonly RateLimiter $rateLimiter = new RateLimiter(),
        bool $sslVerify = true,
        string $baseUri = self::DEFAULT_BASE_URI,
    ) {
        if (empty(trim($apiKey))) {
            throw new \InvalidArgumentException('Vultr API key must not be empty.');
        }

        // Ensure the base URI always ends with a slash so Guzzle resolves paths correctly.
        $baseUri = rtrim($baseUri, '/') . '/';

        $this->http = new Client([
            'base_uri' => $baseUri,
            'timeout'  => self::TIMEOUT,
            'verify'   => $sslVerify,
            'headers'  => [
                'Authorization' => 'Bearer ' . $this->apiKey,
                'Content-Type'  => 'application/json',
                'Accept'        => 'application/json',
                'User-Agent'    => 'vultr-mcp-server/1.0 (php)',
            ],
        ]);
    }

    /**
     * Execute a GET request.
     *
     * @param  array<string, mixed> $query Optional query parameters. Null values are omitted.
     * @return array<string, mixed>
     * @throws ToolCallException On API errors (4xx/5xx).
     */
    public function get(string $path, array $query = []): array
    {
        $cleanQuery = $this->filterNulls($query);

        return $this->request('GET', $path, empty($cleanQuery) ? [] : ['query' => $cleanQuery]);
    }

    /**
     * Execute a POST request.
     *
     * @param  array<string, mixed> $body Request body. Null values are omitted.
     * @return array<string, mixed>
     * @throws ToolCallException On API errors (4xx/5xx).
     */
    public function post(string $path, array $body = []): array
    {
        $cleanBody = $this->filterNulls($body);
        $options   = empty($cleanBody) ? [] : ['json' => $cleanBody];

        return $this->request('POST', $path, $options);
    }

    /**
     * Execute a PATCH request.
     *
     * @param  array<string, mixed> $body Partial update body. Null values are omitted.
     * @return array<string, mixed>
     * @throws ToolCallException On API errors (4xx/5xx).
     */
    public function patch(string $path, array $body = []): array
    {
        $cleanBody = $this->filterNulls($body);

        return $this->request('PATCH', $path, ['json' => $cleanBody]);
    }

    /**
     * Execute a DELETE request.
     *
     * @throws ToolCallException On API errors (4xx/5xx).
     */
    public function delete(string $path): void
    {
        $this->request('DELETE', $path, []);
    }

    /**
     * Execute an HTTP request with rate-limit retry support.
     *
     * @param  array<string, mixed>  $options Guzzle request options.
     * @return array<string, mixed>
     * @throws ToolCallException     On unrecoverable API errors.
     * @throws RateLimitException    Propagated after MAX_RETRIES exhausted.
     */
    private function request(string $method, string $path, array $options): array
    {
        return $this->rateLimiter->withRetry(function () use ($method, $path, $options): array {
            try {
                $response   = $this->http->request($method, ltrim($path, '/'), $options);
                $statusCode = $response->getStatusCode();

                // 204 No Content — success with no body.
                if ($statusCode === 204) {
                    return ['success' => true, 'status' => 204];
                }

                $raw = (string) $response->getBody();

                if ($raw === '') {
                    return ['success' => true, 'status' => $statusCode];
                }

                return json_decode($raw, true, 512, JSON_THROW_ON_ERROR);
            } catch (ClientException $e) {
                $statusCode = $e->getResponse()->getStatusCode();

                if ($statusCode === 429) {
                    $retryAfter = (int) ($e->getResponse()->getHeaderLine('Retry-After') ?: 1);
                    throw new RateLimitException(
                        "Vultr API rate limit hit. Retry after {$retryAfter}s.",
                        $retryAfter,
                        $e,
                    );
                }

                $errorMsg = $this->extractErrorMessage((string) $e->getResponse()->getBody(), $statusCode);
                throw new ToolCallException("Vultr API error ({$statusCode}): {$errorMsg}");
            } catch (ServerException $e) {
                $statusCode = $e->getResponse()->getStatusCode();
                throw new ToolCallException(
                    "Vultr API server error ({$statusCode}). Please try again later.",
                );
            } catch (ConnectException $e) {
                throw new ToolCallException(
                    'Unable to connect to Vultr API: ' . $e->getMessage(),
                );
            } catch (\JsonException $e) {
                throw new ToolCallException('Failed to parse Vultr API response: ' . $e->getMessage());
            }
        });
    }

    /**
     * Extract a human-readable error message from a Vultr error response body.
     */
    private function extractErrorMessage(string $body, int $statusCode): string
    {
        if ($body === '') {
            return "HTTP {$statusCode}";
        }

        try {
            $decoded = json_decode($body, true, 512, JSON_THROW_ON_ERROR);

            return $decoded['error'] ?? $decoded['message'] ?? $body;
        } catch (\JsonException) {
            return $body;
        }
    }

    /**
     * Remove null values from an array (one level deep) so they are not
     * serialised as JSON null or included in query strings.
     *
     * @param  array<string, mixed> $data
     * @return array<string, mixed>
     */
    private function filterNulls(array $data): array
    {
        return array_filter($data, static fn(mixed $v): bool => $v !== null);
    }
}
