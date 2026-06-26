# Comment reduction cleanup for vultr-mcp - PS 5.1 compatible
# Run from repo root

# Helper: read file as string
function ReadFile($path) { [System.IO.File]::ReadAllText((Resolve-Path $path).Path) }
function WriteFile($path, $content) { [System.IO.File]::WriteAllText((Resolve-Path $path).Path, $content) }

# ============================================================
# 1. src/Server.php - Heavy comment reduction
# ============================================================
$f = 'src\Server.php'
$c = ReadFile $f

# Remove section dividers
$c = $c -replace '// -{5,}\r?\n// Bootstrap\r?\n// -{5,}\r?\n', ''
$c = $c -replace '// -{5,}\r?\n// Transport mode detection\r?\n// -{5,}\r?\n', ''
$c = $c -replace '// -{5,}\r?\n// Vultr API key mode detection\r?\n// -{5,}\r?\n', ''
$c = $c -replace '(?s)// VultrClientFactory - creates per-request VultrClient instances\r?\n// -{5,}\r?\n', ''
$c = $c -replace '// -{5,}\r?\n// Tool classes\r?\n// -{5,}\r?\n', ''
$c = $c -replace '(?s)// PSR-11 container - supplies pre-wired instances.*?\r?\n// -{5,}\r?\n', ''
$c = $c -replace '(?s)// MCP Server construction - manual tool registration\r?\n// -{5,}\r?\n', ''
$c = $c -replace '// -{5,}\r?\n// Run with the selected transport\r?\n// -{5,}\r?\n', ''

# Remove verbose inline comments
$c = $c -replace '// Autoload may already be loaded in FrankenPHP worker mode\r?\n', ''
$c = $c -replace '(?s)// Load environment variables.*?In FrankenPHP worker mode, this runs once at worker boot\.\r?\n', ''
$c = $c -replace '(?s)// VULTR_MCP_TRANSPORT controls which transport to use:.*?// since there.*?messages\. Otherwise default\r?\n// to STDIO so that `php src/Server\.php` just works when launched by an MCP client\.\r?\n', ''
$c = $c -replace '(?s)// Auto-detect transport mode:.*?//   - Otherwise assume STDIO \(MCP client piping JSON-RPC\)\r?\n', ''

# Remove verbose per-user mode comments
$c = $c -replace '(?s)// In STDIO mode, per-user is forced on.*?// In HTTP mode, per-user requires the X-Vultr-API-Key header per request\.\r?\n', ''
$c = $c -replace '(?s)// STDIO: always use per-user mode\..*?\r?\n', ''
$c = $c -replace '(?s)// If VULTR_API_KEY is set, treat it as the default for this process\.\r?\n', ''
$c = $c -replace '// HTTP legacy mode requires a global API key\.\r?\n', ''

# Remove verbose session store comment
$c = $c -replace '(?s)// Session persistence for HTTP transport\..*?emptyDir volume in K8s\.\r?\n', ''

# Remove "Per-request handler" comment
$c = $c -replace '// Per-request handler - runs for each incoming HTTP request\r?\n', ''

# Remove "Emit PSR-7 response" comment
$c = $c -replace '// Emit PSR-7 response\r?\n', ''

# Remove verbose STDIO inline comments (already trimmed, but clean remaining)
$c = $c -replace '// Reads JSON-RPC from STDIN, writes responses to STDOUT\.\r?\n', ''
$c = $c -replace '(?s)// Used by Claude Desktop, VS Code Copilot, Cursor.*?that spawn the server as a child process\.\r?\n', ''
$c = $c -replace '(?s)// Auth: Not needed.*?no network exposure\.\r?\n', ''
$c = $c -replace '// API key: Read from VULTR_API_KEY env var \(set above in RequestContext\)\.\r?\n', ''

WriteFile $f $c

# ============================================================
# 2. src/Auth/VultrAuth.php - Moderate cuts
# ============================================================
$f = 'src\Auth\VultrAuth.php'
$c = ReadFile $f

# Remove verbose constructor param docblock
$c = $c -replace '(?s)/\*\*\r?\n     \* @param ResponseFactoryInterface \$responseFactory PSR-17 factory for creating error responses\.\r?\n     \* @param string\|null              \$expectedToken   Expected bearer token value\.\r?\n     \*                                                  Pass null or empty string to skip authentication\.\r?\n     \* @param bool                     \$perUserMode     When true, require X-Vultr-API-Key header\.\r?\n     \*/', '/***/'

# Remove step dividers
$c = $c -replace '// --- Step 1: Validate MCP-level bearer token \(if configured\) ---\r?\n', ''
$c = $c -replace '// --- Step 2: Extract per-user Vultr API key ---\r?\n', ''

# Remove redundant "Always allow OPTIONS" comment
$c = $c -replace '// Always allow OPTIONS \(CORS pre-flight\) through unauthenticated\.\r?\n', ''

# Remove redundant "Constant-time comparison" inline comment (docblock already says it)
$c = $c -replace '        // Constant-time comparison to prevent timing attacks\.\r?\n', ''

# Remove redundant "Fallback 1:" comment
$c = $c -replace '// Fallback 1:\r?\n', ''

WriteFile $f $c

# ============================================================
# 3. src/Utils/VultrClientFactory.php - Light cuts
# ============================================================
$f = 'src\Utils\VultrClientFactory.php'
$c = ReadFile $f

# Remove @var annotation on defaultApiKey
$c = $c -replace '    /\*\* @var string\|null Cached single-key mode API key \*/\r?\n', ''

# Remove redundant @param on perUserMode
$c = $c -replace '     \* @param bool \$perUserMode When true, the API key is read from RequestContext per-request\.\r?\n', ''

WriteFile $f $c

# ============================================================
# 4. src/Utils/RequestContext.php - Remove method docblocks that restate method name
# ============================================================
$f = 'src\Utils\RequestContext.php'
$c = ReadFile $f

$c = $c -replace '    /\*\*\r?\n     \* Set the Vultr API key for the current request\.\r?\n     \*/\r?\n', ''
$c = $c -replace '    /\*\*\r?\n     \* Get the Vultr API key for the current request\.\r?\n     \*/\r?\n', ''
$c = $c -replace '    /\*\*\r?\n     \* Clear the request context \(call at the end of request processing\)\.\r?\n     \*/\r?\n', ''

WriteFile $f $c

# ============================================================
# 5. bin/console - Light cuts
# ============================================================
$f = 'bin\console'
$c = ReadFile $f

$c = $c -replace '// Load \.env if present\r?\n', ''
$c = $c -replace '// Force STDIO mode\r?\n', ''
$c = $c -replace '// Delegate to Server\.php which detects STDIO transport\r?\n', ''

WriteFile $f $c

# ============================================================
# 6. src/Generator/OpenApiGenerator.php - Light cuts
# ============================================================
$f = 'src\Generator\OpenApiGenerator.php'
$c = ReadFile $f

# Remove "Skip operations not belonging to this tag" comment
$c = $c -replace '// Skip operations not belonging to this tag\.\r?\n', ''
# Remove "Skip deprecated operations" comment
$c = $c -replace '// Skip deprecated operations\.\r?\n', ''
# Remove "Optional body params" and "Optional query params" comments
$c = $c -replace '// Optional body params \(nullable with null default\)\.\r?\n', ''
$c = $c -replace '// Optional query params \(always optional\)\.\r?\n', ''

WriteFile $f $c

# ============================================================
# 7. README.md - Fix outdated info
# ============================================================
$f = 'README.md'
$c = ReadFile $f

# Fix remote URL
$c = $c -replace 'https://mcp\.vrnd\.io', 'https://vultrmcp.com'

# Fix PHP version requirement
$c = $c -replace 'PHP 8\.1\+', 'PHP 8.4+'

# Fix "Stateless" claim - now uses sessions
$c = $c -replace 'Stateless - no session persistence, safe for horizontal scaling', 'Session-based (FileSessionStore); single-replica recommended unless shared storage is configured'

# Fix bin/generate.php reference
$c = $c -replace 'Re-run `php bin/generate\.php` to regenerate', 'Re-run `php bin/console mcp:generate` to regenerate'

# Fix project structure - replace outdated directory tree
$c = $c -replace '(?s)```\r?\nvultr-mcp/\r?\n.*?```\r?\n', ''

WriteFile $f $c

Write-Host 'Comment reduction complete. Review with: git diff'
