<?php

declare(strict_types=1);

namespace Vultr\Mcp\Utils;

/**
 * Provides exponential-backoff retry logic for callables that may hit
 * Vultr's 30 req/sec rate limit (HTTP 429).
 *
 * Jitter (±10 %) is applied to each delay to avoid thundering-herd
 * situations when multiple clients back off simultaneously.
 */
final class RateLimiter
{
    /** Maximum number of retry attempts after the initial call. */
    private const MAX_RETRIES = 5;

    /** Base delay in milliseconds for exponential backoff. */
    private const BASE_DELAY_MS = 200;

    /** Hard cap on computed delay in milliseconds. */
    private const MAX_DELAY_MS = 30_000;

    /**
     * Execute $fn, retrying up to MAX_RETRIES times on {@see RateLimitException}.
     *
     * @template T
     * @param  callable(): T $fn
     * @return T
     * @throws RateLimitException When MAX_RETRIES is exhausted.
     * @throws \Throwable         Any non-rate-limit exception propagates immediately.
     */
    public function withRetry(callable $fn): mixed
    {
        $attempt = 0;

        while (true) {
            try {
                return $fn();
            } catch (RateLimitException $e) {
                if ($attempt >= self::MAX_RETRIES) {
                    throw $e;
                }

                // Prefer the server-supplied Retry-After value; fall back to exponential backoff.
                if ($e->retryAfter > 0) {
                    $delayMs = $e->retryAfter * 1_000;
                } else {
                    $delayMs = min(self::MAX_DELAY_MS, self::BASE_DELAY_MS * (2 ** $attempt));
                }

                // Add ±10 % jitter.
                $jitterMs = (int) ($delayMs * 0.1 * ((mt_rand() / mt_getrandmax()) * 2 - 1));
                $delayMs  = max(1, $delayMs + $jitterMs);

                usleep($delayMs * 1_000); // usleep expects microseconds
                ++$attempt;
            }
        }
    }
}
