#!/usr/bin/env php
<?php

declare(strict_types=1);

/**
 * CLI entry point for OpenApiGenerator.
 *
 * Usage:
 *   php bin/generate.php [options]
 *
 * Options:
 *   --spec=<path>     Path to Vultr OpenAPI JSON file.
 *                     Defaults to: openapi.json in the project root.
 *   --tags=<list>     Comma-separated list of tags to generate.
 *                     Defaults to: instances,baremetal
 *   --output=<dir>    Output directory for generated PHP files.
 *                     Defaults to: src/Tools/
 *
 * Examples:
 *   php bin/generate.php
 *   php bin/generate.php --spec=openapi.json --tags=instances,baremetal
 *   php bin/generate.php --spec=/tmp/vultr-openapi.json --output=src/Tools/
 */

$root = dirname(__DIR__);

require_once $root . '/vendor/autoload.php';

use Vultr\Mcp\Generator\OpenApiGenerator;

// ---------------------------------------------------------------------------
// Parse CLI arguments
// ---------------------------------------------------------------------------

$opts = getopt('', ['spec::', 'tags::', 'output::']);

$specPath  = $opts['spec']   ?? $root . '/openapi.json';
$tagsRaw   = $opts['tags']   ?? 'instances,baremetal';
$outputDir = $opts['output'] ?? $root . '/src/Tools';
$tags      = array_filter(array_map('trim', explode(',', $tagsRaw)));

if (!file_exists($specPath)) {
    fwrite(STDERR, "Error: OpenAPI spec file not found: {$specPath}\n");
    fwrite(STDERR, "Provide the Vultr OpenAPI JSON via --spec=<path>.\n");
    exit(1);
}

if (empty($tags)) {
    fwrite(STDERR, "Error: --tags must be a non-empty comma-separated list.\n");
    exit(1);
}

// ---------------------------------------------------------------------------
// Generate
// ---------------------------------------------------------------------------

try {
    $generator = new OpenApiGenerator($specPath);
    $generator->generate($tags, $outputDir);
    echo PHP_EOL . 'Done. Review and commit the generated files.' . PHP_EOL;
} catch (\Throwable $e) {
    fwrite(STDERR, 'Generation failed: ' . $e->getMessage() . PHP_EOL);
    exit(1);
}
