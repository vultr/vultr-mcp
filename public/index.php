<?php

declare(strict_types=1);

// Force HTTP transport mode (FrankenPHP never has STDIN as a pipe)
putenv('VULTR_MCP_TRANSPORT=http');

require_once dirname(__DIR__).'/src/Server.php';