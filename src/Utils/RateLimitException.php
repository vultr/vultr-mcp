<?php

declare(strict_types=1);

namespace Vultr\Mcp\Utils;

/**
 * Thrown when the Vultr API responds with HTTP 429 Too Many Requests.
 *
 * Carries the Retry-After value from the response header so the
 * {@see RateLimiter} can honour the server-supplied back-off interval.
 */
final class RateLimitException extends \RuntimeException
{
    /**
     * @param int $retryAfter Seconds to wait before the next attempt (from Retry-After header).
     */
    public function __construct(
        string $message,
        public readonly int $retryAfter = 1,
        ?\Throwable $previous = null,
    ) {
        parent::__construct($message, 429, $previous);
    }
}
