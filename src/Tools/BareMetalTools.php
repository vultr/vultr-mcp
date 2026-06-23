<?php

declare(strict_types=1);

namespace Vultr\Mcp\Tools;

use Mcp\Capability\Attribute\McpTool;
use Mcp\Exception\ToolCallException;
use Vultr\Mcp\Utils\VultrClient;
use Vultr\Mcp\Utils\VultrClientFactory;

/**
 * MCP tools for Vultr Bare Metal instances (/v2/bare-metals).
 *
 * Auto-generated from the Vultr OpenAPI spec via {@see \Vultr\Mcp\Generator\OpenApiGenerator}.
 * Covers all CRUD operations and power-state actions for Bare Metal instances.
 *
 * All optional parameters default to null and are omitted from the API request
 * when not provided. Required parameters are explicitly typed without defaults.
 */
final class BareMetalTools
{
    /**
     * @param VultrClientFactory $clientFactory Factory for per-request VultrClient instances.
     */
    public function __construct(
        private readonly VultrClientFactory $clientFactory,
    ) {}

    /**
     * Resolve the VultrClient for the current request.
     *
     * In per-user mode the factory creates a client with the user's API key.
     * In legacy mode the factory uses the configured default key.
     */
    private function getClient(): VultrClient
    {
        return $this->clientFactory->create();
    }

    // -------------------------------------------------------------------------
    // Collection operations
    // -------------------------------------------------------------------------

    /**
     * List all Bare Metal instances in your account.
     *
     * Returns a paginated list of bare metal instances. Use `cursor` to navigate pages.
     *
     * @param  int|null    $perPage Number of items per page (default 100, max 500).
     * @param  string|null $cursor  Pagination cursor from a previous response's meta.links.next.
     * @return array<string, mixed>
     */
    #[McpTool(name: 'list_bare_metals')]
    public function listBareMetals(?int $perPage = null, ?string $cursor = null): array
    {
        return $this->getClient()->get('/bare-metals', [
            'per_page' => $perPage,
            'cursor'   => $cursor,
        ]);
    }

    /**
     * Create a new Bare Metal instance.
     *
     * Deploy a new bare metal server in `region` using `plan`. Choose exactly one boot
     * source: `osId`, `snapshotId`, `appId`, or `imageId`.
     *
     * @param  string      $region          Region ID where the instance will be created (e.g. "ams").
     * @param  string      $plan            Bare Metal plan ID (e.g. "vbm-4c-32gb").
     * @param  int|null    $osId            Operating system ID.
     * @param  string|null $scriptId        Startup script ID to run on first boot.
     * @param  bool|null   $enableIpv6      Enable IPv6 networking.
     * @param  array|null  $sshkeyId        Array of SSH key IDs to install.
     * @param  string|null $userData        Base64-encoded user data.
     * @param  string|null $label           User-supplied label.
     * @param  bool|null   $activationEmail Send activation email after deployment.
     * @param  string|null $hostname        Hostname for the instance.
     * @param  string|null $reservedIpv4    Reserved IPv4 ID.
     * @param  int|null    $appId           Application ID.
     * @param  string|null $imageId         Application image ID.
     * @param  string|null $snapshotId      Snapshot ID to restore from.
     * @param  string|null $ipxeChainUrl    iPXE chainloader URL (requires osId = 159).
     * @param  bool|null   $persistentPxe   Enable persistent PXE booting.
     * @param  array|null  $attachVpc       Array of VPC IDs to attach.
     * @param  bool|null   $enableVpc       Attach a single VPC (auto-created if none in region).
     * @param  array|null  $tags            Array of string tags to apply.
     * @param  string|null $userScheme      Linux user scheme: "root" (default) or "limited".
     * @param  string|null $mdiskMode       RAID mode: "raid1", "jbod", or "none" (default).
     * @param  array|null  $appVariables    Marketplace app variable inputs (key/value pairs).
     * @return array<string, mixed>
     */
    #[McpTool(name: 'create_bare_metal')]
    public function createBareMetal(
        string $region,
        string $plan,
        ?int $osId = null,
        ?string $scriptId = null,
        ?bool $enableIpv6 = null,
        ?array $sshkeyId = null,
        ?string $userData = null,
        ?string $label = null,
        ?bool $activationEmail = null,
        ?string $hostname = null,
        ?string $reservedIpv4 = null,
        ?int $appId = null,
        ?string $imageId = null,
        ?string $snapshotId = null,
        ?string $ipxeChainUrl = null,
        ?bool $persistentPxe = null,
        ?array $attachVpc = null,
        ?bool $enableVpc = null,
        ?array $tags = null,
        ?string $userScheme = null,
        ?string $mdiskMode = null,
        ?array $appVariables = null,
    ): array {
        if (!in_array($userScheme, [null, 'root', 'limited'], true)) {
            throw new ToolCallException("Invalid userScheme '{$userScheme}'. Must be 'root' or 'limited'.");
        }

        if (!in_array($mdiskMode, [null, 'raid1', 'jbod', 'none'], true)) {
            throw new ToolCallException("Invalid mdiskMode '{$mdiskMode}'. Must be 'raid1', 'jbod', or 'none'.");
        }

        return $this->getClient()->post('/bare-metals', [
            'region'           => $region,
            'plan'             => $plan,
            'os_id'            => $osId,
            'script_id'        => $scriptId,
            'enable_ipv6'      => $enableIpv6,
            'sshkey_id'        => $sshkeyId,
            'user_data'        => $userData,
            'label'            => $label,
            'activation_email' => $activationEmail,
            'hostname'         => $hostname,
            'reserved_ipv4'    => $reservedIpv4,
            'app_id'           => $appId,
            'image_id'         => $imageId,
            'snapshot_id'      => $snapshotId,
            'ipxe_chain_url'   => $ipxeChainUrl,
            'persistent_pxe'   => $persistentPxe,
            'attach_vpc'       => $attachVpc,
            'enable_vpc'       => $enableVpc,
            'tags'             => $tags,
            'user_scheme'      => $userScheme,
            'mdisk_mode'       => $mdiskMode,
            'app_variables'    => $appVariables,
        ]);
    }

    // -------------------------------------------------------------------------
    // Single-instance operations
    // -------------------------------------------------------------------------

    /**
     * Get information about a specific Bare Metal instance.
     *
     * @param  string $baremetalId The Bare Metal instance ID (UUID).
     * @return array<string, mixed>
     */
    #[McpTool(name: 'get_bare_metal')]
    public function getBareMetal(string $baremetalId): array
    {
        return $this->getClient()->get("/bare-metals/{$baremetalId}");
    }

    /**
     * Update a Bare Metal instance. All attributes are optional; unset attributes retain their original values.
     *
     * Note: Changing osId, appId, or imageId may take a few extra seconds to complete.
     *       Changes to userScheme and mdiskMode require a reinstall to take effect.
     *
     * @param  string      $baremetalId  The Bare Metal instance ID to update.
     * @param  string|null $label        New user-supplied label.
     * @param  bool|null   $enableIpv6   Enable IPv6 networking.
     * @param  int|null    $osId         Reinstall with this operating system ID.
     * @param  int|null    $appId        Reinstall with this application ID.
     * @param  string|null $imageId      Reinstall with this application image ID.
     * @param  string|null $userData     Base64-encoded user data.
     * @param  array|null  $tags         Replacement tag set.
     * @param  string|null $userScheme   User scheme: "root" or "limited". Requires reinstall.
     * @param  string|null $mdiskMode    RAID mode: "raid1", "jbod", or "none". Requires reinstall.
     * @param  string|null $ipxeChainUrl iPXE chainloader URL.
     * @return array<string, mixed>
     */
    #[McpTool(name: 'update_bare_metal')]
    public function updateBareMetal(
        string $baremetalId,
        ?string $label = null,
        ?bool $enableIpv6 = null,
        ?int $osId = null,
        ?int $appId = null,
        ?string $imageId = null,
        ?string $userData = null,
        ?array $tags = null,
        ?string $userScheme = null,
        ?string $mdiskMode = null,
        ?string $ipxeChainUrl = null,
    ): array {
        if (!in_array($userScheme, [null, 'root', 'limited'], true)) {
            throw new ToolCallException("Invalid userScheme '{$userScheme}'. Must be 'root' or 'limited'.");
        }

        if (!in_array($mdiskMode, [null, 'raid1', 'jbod', 'none'], true)) {
            throw new ToolCallException("Invalid mdiskMode '{$mdiskMode}'. Must be 'raid1', 'jbod', or 'none'.");
        }

        return $this->getClient()->patch("/bare-metals/{$baremetalId}", [
            'label'          => $label,
            'enable_ipv6'    => $enableIpv6,
            'os_id'          => $osId,
            'app_id'         => $appId,
            'image_id'       => $imageId,
            'user_data'      => $userData,
            'tags'           => $tags,
            'user_scheme'    => $userScheme,
            'mdisk_mode'     => $mdiskMode,
            'ipxe_chain_url' => $ipxeChainUrl,
        ]);
    }

    /**
     * Permanently delete a Bare Metal instance.
     *
     * WARNING: This action is irreversible. All data on the instance will be lost.
     *
     * @param  string $baremetalId The Bare Metal instance ID to delete.
     * @return array<string, mixed> Success confirmation.
     */
    #[McpTool(name: 'delete_bare_metal')]
    public function deleteBareMetal(string $baremetalId): array
    {
        $this->getClient()->delete("/bare-metals/{$baremetalId}");

        return ['success' => true, 'message' => "Bare Metal instance {$baremetalId} has been deleted."];
    }

    /**
     * Start a stopped Bare Metal instance.
     *
     * @param  string $baremetalId The Bare Metal instance ID to start.
     * @return array<string, mixed> Success confirmation.
     */
    #[McpTool(name: 'start_bare_metal')]
    public function startBareMetal(string $baremetalId): array
    {
        $result = $this->getClient()->post("/bare-metals/{$baremetalId}/start");

        return array_merge($result, ['message' => "Bare Metal instance {$baremetalId} start initiated."]);
    }

    /**
     * Reboot a Bare Metal instance.
     *
     * @param  string $baremetalId The Bare Metal instance ID to reboot.
     * @return array<string, mixed> Success confirmation.
     */
    #[McpTool(name: 'reboot_bare_metal')]
    public function rebootBareMetal(string $baremetalId): array
    {
        $result = $this->getClient()->post("/bare-metals/{$baremetalId}/reboot");

        return array_merge($result, ['message' => "Bare Metal instance {$baremetalId} reboot initiated."]);
    }

    /**
     * Reinstall a Bare Metal instance using an optional new hostname.
     *
     * Note: This action may take some time to complete. All data will be erased.
     *
     * @param  string      $baremetalId The Bare Metal instance ID to reinstall.
     * @param  string|null $hostname    Optional new hostname to use after reinstall.
     * @return array<string, mixed>
     */
    #[McpTool(name: 'reinstall_bare_metal')]
    public function reinstallBareMetal(string $baremetalId, ?string $hostname = null): array
    {
        return $this->getClient()->post("/bare-metals/{$baremetalId}/reinstall", [
            'hostname' => $hostname,
        ]);
    }

    /**
     * Halt (power off) a Bare Metal instance immediately, without a graceful shutdown.
     *
     * @param  string $baremetalId The Bare Metal instance ID to halt.
     * @return array<string, mixed> Success confirmation.
     */
    #[McpTool(name: 'halt_bare_metal')]
    public function haltBareMetal(string $baremetalId): array
    {
        $result = $this->getClient()->post("/bare-metals/{$baremetalId}/halt");

        return array_merge($result, ['message' => "Bare Metal instance {$baremetalId} halt initiated."]);
    }

    // -------------------------------------------------------------------------
    // Instance metadata / sub-resources
    // -------------------------------------------------------------------------

    /**
     * Get the IPv4 addresses assigned to a Bare Metal instance.
     *
     * @param  string $baremetalId The Bare Metal instance ID.
     * @return array<string, mixed>
     */
    #[McpTool(name: 'get_bare_metal_ipv4')]
    public function getBareMetalIpv4(string $baremetalId): array
    {
        return $this->getClient()->get("/bare-metals/{$baremetalId}/ipv4");
    }

    /**
     * Get the IPv6 addresses assigned to a Bare Metal instance.
     *
     * @param  string $baremetalId The Bare Metal instance ID.
     * @return array<string, mixed>
     */
    #[McpTool(name: 'get_bare_metal_ipv6')]
    public function getBareMetalIpv6(string $baremetalId): array
    {
        return $this->getClient()->get("/bare-metals/{$baremetalId}/ipv6");
    }

    /**
     * Get bandwidth usage for a Bare Metal instance.
     *
     * @param  string $baremetalId The Bare Metal instance ID.
     * @return array<string, mixed>
     */
    #[McpTool(name: 'get_bare_metal_bandwidth')]
    public function getBareMetalBandwidth(string $baremetalId): array
    {
        return $this->getClient()->get("/bare-metals/{$baremetalId}/bandwidth");
    }

    /**
     * Get the user data attached to a Bare Metal instance.
     *
     * Returns base64-encoded user data.
     *
     * @param  string $baremetalId The Bare Metal instance ID.
     * @return array<string, mixed>
     */
    #[McpTool(name: 'get_bare_metal_user_data')]
    public function getBareMetalUserData(string $baremetalId): array
    {
        return $this->getClient()->get("/bare-metals/{$baremetalId}/user-data");
    }

    /**
     * List available upgrades for a Bare Metal instance.
     *
     * @param  string      $baremetalId The Bare Metal instance ID.
     * @param  string|null $type        Upgrade type filter (e.g. "all").
     * @return array<string, mixed>
     */
    #[McpTool(name: 'get_bare_metal_upgrades')]
    public function getBareMetalUpgrades(string $baremetalId, ?string $type = null): array
    {
        return $this->getClient()->get("/bare-metals/{$baremetalId}/upgrades", [
            'type' => $type,
        ]);
    }
}
