<?php

declare(strict_types=1);

namespace Vultr\Mcp\Tools;

use Mcp\Capability\Attribute\McpTool;
use Mcp\Exception\ToolCallException;
use Vultr\Mcp\Utils\VultrClient;

/**
 * MCP tools for Vultr VPS Instances (/v2/instances).
 *
 * Auto-generated from the Vultr OpenAPI spec via {@see \Vultr\Mcp\Generator\OpenApiGenerator}.
 * Covers all CRUD operations and power-state actions for VPS instances.
 *
 * All optional parameters default to null and are omitted from the API request
 * when not provided. Required parameters are explicitly typed without defaults.
 */
final class InstanceTools
{
    public function __construct(
        private readonly VultrClient $client,
    ) {}

    // -------------------------------------------------------------------------
    // Collection operations
    // -------------------------------------------------------------------------

    /**
     * List all VPS instances in your account.
     *
     * Returns a paginated list of instances. Use `cursor` to navigate pages.
     *
     * @param  int|null    $perPage          Number of items per page (default 100, max 500).
     * @param  string|null $cursor           Pagination cursor from a previous response's meta.links.next.
     * @param  string|null $label            Filter by instance label.
     * @param  string|null $mainIp           Filter by main IP address.
     * @param  string|null $region           Filter by region ID (e.g. "ewr").
     * @param  string|null $firewallGroupId  Filter by firewall group ID.
     * @param  string|null $hostname         Filter by hostname.
     * @param  bool|null   $showPendingCharges Include pending charges in the response.
     * @return array<string, mixed>
     */
    #[McpTool(name: 'list_instances')]
    public function listInstances(
        ?int $perPage = null,
        ?string $cursor = null,
        ?string $label = null,
        ?string $mainIp = null,
        ?string $region = null,
        ?string $firewallGroupId = null,
        ?string $hostname = null,
        ?bool $showPendingCharges = null,
    ): array {
        return $this->client->get('/instances', [
            'per_page'             => $perPage,
            'cursor'               => $cursor,
            'label'                => $label,
            'main_ip'              => $mainIp,
            'region'               => $region,
            'firewall_group_id'    => $firewallGroupId,
            'hostname'             => $hostname,
            'show_pending_charges' => $showPendingCharges,
        ]);
    }

    /**
     * Create a new VPS Instance.
     *
     * Deploy a new instance in `region` using `plan`. Choose exactly one boot
     * source: `osId`, `isoId`, `snapshotId`, `appId`, or `imageId`.
     *
     * @param  string       $region           Region ID where the instance will be created (e.g. "ewr").
     * @param  string       $plan             Plan ID (e.g. "vc2-6c-16gb").
     * @param  int|null     $osId             Operating system ID.
     * @param  string|null  $isoId            ISO ID to boot from.
     * @param  string|null  $scriptId         Startup script ID to run on first boot.
     * @param  string|null  $snapshotId       Snapshot ID to restore from.
     * @param  bool|null    $enableIpv6       Enable IPv6 networking.
     * @param  bool|null    $disablePublicIpv4 Disable public IPv4 (only with enableIpv6=true).
     * @param  string|null  $label            User-supplied label.
     * @param  array|null   $sshkeyId         Array of SSH key IDs to install.
     * @param  string|null  $backups          Enable automatic backups: "enabled" or "disabled".
     * @param  int|null     $appId            Application ID.
     * @param  string|null  $imageId          Application image ID.
     * @param  string|null  $userData         Base64-encoded user data.
     * @param  bool|null    $ddosProtection   Enable DDoS protection (additional charge).
     * @param  bool|null    $activationEmail  Send activation email after deployment.
     * @param  string|null  $hostname         Hostname for the instance.
     * @param  string|null  $firewallGroupId  Firewall group ID to attach.
     * @param  string|null  $reservedIpv4     Reserved IPv4 ID to use as the main IP.
     * @param  bool|null    $enableVpc        Attach a VPC (auto-created if none exists in region).
     * @param  array|null   $attachVpc        Array of VPC IDs to attach.
     * @param  bool|null    $vpcOnly          Instance will have no public IP; must attach a VPC with NAT gateway.
     * @param  array|null   $tags             Array of string tags to apply.
     * @param  string|null  $userScheme       Linux user scheme: "root" (default) or "limited".
     * @param  string|null  $ipxeChainUrl     iPXE chainloader URL.
     * @param  object|null  $appVariables     Marketplace app variable inputs (key/value pairs).
     * @return array<string, mixed>
     */
    #[McpTool(name: 'create_instance')]
    public function createInstance(
        string $region,
        string $plan,
        ?int $osId = null,
        ?string $isoId = null,
        ?string $scriptId = null,
        ?string $snapshotId = null,
        ?bool $enableIpv6 = null,
        ?bool $disablePublicIpv4 = null,
        ?string $label = null,
        ?array $sshkeyId = null,
        ?string $backups = null,
        ?int $appId = null,
        ?string $imageId = null,
        ?string $userData = null,
        ?bool $ddosProtection = null,
        ?bool $activationEmail = null,
        ?string $hostname = null,
        ?string $firewallGroupId = null,
        ?string $reservedIpv4 = null,
        ?bool $enableVpc = null,
        ?array $attachVpc = null,
        ?bool $vpcOnly = null,
        ?array $tags = null,
        ?string $userScheme = null,
        ?string $ipxeChainUrl = null,
        ?array $appVariables = null,
    ): array {
        if (!in_array($userScheme, [null, 'root', 'limited'], true)) {
            throw new ToolCallException("Invalid userScheme '{$userScheme}'. Must be 'root' or 'limited'.");
        }

        if (!in_array($backups, [null, 'enabled', 'disabled'], true)) {
            throw new ToolCallException("Invalid backups value '{$backups}'. Must be 'enabled' or 'disabled'.");
        }

        return $this->client->post('/instances', [
            'region'               => $region,
            'plan'                 => $plan,
            'os_id'                => $osId,
            'iso_id'               => $isoId,
            'script_id'            => $scriptId,
            'snapshot_id'          => $snapshotId,
            'enable_ipv6'          => $enableIpv6,
            'disable_public_ipv4'  => $disablePublicIpv4,
            'label'                => $label,
            'sshkey_id'            => $sshkeyId,
            'backups'              => $backups,
            'app_id'               => $appId,
            'image_id'             => $imageId,
            'user_data'            => $userData,
            'ddos_protection'      => $ddosProtection,
            'activation_email'     => $activationEmail,
            'hostname'             => $hostname,
            'firewall_group_id'    => $firewallGroupId,
            'reserved_ipv4'        => $reservedIpv4,
            'enable_vpc'           => $enableVpc,
            'attach_vpc'           => $attachVpc,
            'vpc_only'             => $vpcOnly,
            'tags'                 => $tags,
            'user_scheme'          => $userScheme,
            'ipxe_chain_url'       => $ipxeChainUrl,
            'app_variables'        => $appVariables,
        ]);
    }

    // -------------------------------------------------------------------------
    // Single-instance operations
    // -------------------------------------------------------------------------

    /**
     * Get information about a specific VPS instance.
     *
     * @param  string $instanceId The instance ID (UUID).
     * @return array<string, mixed>
     */
    #[McpTool(name: 'get_instance')]
    public function getInstance(string $instanceId): array
    {
        return $this->client->get("/instances/{$instanceId}");
    }

    /**
     * Update a VPS instance. All attributes are optional; unset attributes retain their original values.
     *
     * Note: Changing osId, appId, or imageId may take a few extra seconds to complete.
     *
     * @param  string       $instanceId      The instance ID to update.
     * @param  string|null  $label           New user-supplied label.
     * @param  string|null  $plan            Upgrade to this plan ID.
     * @param  string|null  $backups         Backup state: "enabled" or "disabled".
     * @param  bool|null    $enableIpv6      Enable IPv6.
     * @param  bool|null    $ddosProtection  Enable/disable DDoS protection.
     * @param  string|null  $firewallGroupId Firewall group ID to attach.
     * @param  int|null     $osId            Reinstall with this OS ID.
     * @param  int|null     $appId           Reinstall with this application ID.
     * @param  string|null  $imageId         Reinstall with this application image ID.
     * @param  string|null  $userData        Base64-encoded user data.
     * @param  array|null   $tags            Replacement tag set.
     * @param  array|null   $attachVpc       Array of VPC IDs to attach.
     * @param  array|null   $detachVpc       Array of VPC IDs to detach.
     * @param  bool|null    $enableVpc       Attach a single VPC (auto-created if none in region).
     * @param  string|null  $userScheme      User scheme: "root" or "limited". Requires reinstall.
     * @return array<string, mixed>
     */
    #[McpTool(name: 'update_instance')]
    public function updateInstance(
        string $instanceId,
        ?string $label = null,
        ?string $plan = null,
        ?string $backups = null,
        ?bool $enableIpv6 = null,
        ?bool $ddosProtection = null,
        ?string $firewallGroupId = null,
        ?int $osId = null,
        ?int $appId = null,
        ?string $imageId = null,
        ?string $userData = null,
        ?array $tags = null,
        ?array $attachVpc = null,
        ?array $detachVpc = null,
        ?bool $enableVpc = null,
        ?string $userScheme = null,
    ): array {
        if (!in_array($userScheme, [null, 'root', 'limited'], true)) {
            throw new ToolCallException("Invalid userScheme '{$userScheme}'. Must be 'root' or 'limited'.");
        }

        if (!in_array($backups, [null, 'enabled', 'disabled'], true)) {
            throw new ToolCallException("Invalid backups value '{$backups}'. Must be 'enabled' or 'disabled'.");
        }

        return $this->client->patch("/instances/{$instanceId}", [
            'label'              => $label,
            'plan'               => $plan,
            'backups'            => $backups,
            'enable_ipv6'        => $enableIpv6,
            'ddos_protection'    => $ddosProtection,
            'firewall_group_id'  => $firewallGroupId,
            'os_id'              => $osId,
            'app_id'             => $appId,
            'image_id'           => $imageId,
            'user_data'          => $userData,
            'tags'               => $tags,
            'attach_vpc'         => $attachVpc,
            'detach_vpc'         => $detachVpc,
            'enable_vpc'         => $enableVpc,
            'user_scheme'        => $userScheme,
        ]);
    }

    /**
     * Permanently delete a VPS instance.
     *
     * WARNING: This action is irreversible. All data on the instance will be lost.
     *
     * @param  string $instanceId The instance ID to delete.
     * @return array<string, mixed> Success confirmation.
     */
    #[McpTool(name: 'delete_instance')]
    public function deleteInstance(string $instanceId): array
    {
        $this->client->delete("/instances/{$instanceId}");

        return ['success' => true, 'message' => "Instance {$instanceId} has been deleted."];
    }

    /**
     * Start a stopped VPS instance.
     *
     * @param  string $instanceId The instance ID to start.
     * @return array<string, mixed> Success confirmation.
     */
    #[McpTool(name: 'start_instance')]
    public function startInstance(string $instanceId): array
    {
        $result = $this->client->post("/instances/{$instanceId}/start");

        return array_merge($result, ['message' => "Instance {$instanceId} start initiated."]);
    }

    /**
     * Reboot a VPS instance (graceful restart).
     *
     * @param  string $instanceId The instance ID to reboot.
     * @return array<string, mixed> Success confirmation.
     */
    #[McpTool(name: 'reboot_instance')]
    public function rebootInstance(string $instanceId): array
    {
        $result = $this->client->post("/instances/{$instanceId}/reboot");

        return array_merge($result, ['message' => "Instance {$instanceId} reboot initiated."]);
    }

    /**
     * Reinstall a VPS instance using its current OS or a new hostname.
     *
     * Note: This action may take a few extra seconds to complete. All data will be erased.
     *
     * @param  string      $instanceId The instance ID to reinstall.
     * @param  string|null $hostname   Optional new hostname to use after reinstall.
     * @return array<string, mixed>
     */
    #[McpTool(name: 'reinstall_instance')]
    public function reinstallInstance(string $instanceId, ?string $hostname = null): array
    {
        return $this->client->post("/instances/{$instanceId}/reinstall", [
            'hostname' => $hostname,
        ]);
    }

    /**
     * Halt (power off) a VPS instance immediately, without a graceful shutdown.
     *
     * @param  string $instanceId The instance ID to halt.
     * @return array<string, mixed> Success confirmation.
     */
    #[McpTool(name: 'halt_instance')]
    public function haltInstance(string $instanceId): array
    {
        $result = $this->client->post("/instances/{$instanceId}/halt");

        return array_merge($result, ['message' => "Instance {$instanceId} halt initiated."]);
    }

    // -------------------------------------------------------------------------
    // Bulk operations
    // -------------------------------------------------------------------------

    /**
     * Start multiple VPS instances simultaneously.
     *
     * @param  array $instanceIds Array of instance IDs (UUIDs) to start.
     * @return array<string, mixed> Success confirmation.
     */
    #[McpTool(name: 'start_instances')]
    public function startInstances(array $instanceIds): array
    {
        if (empty($instanceIds)) {
            throw new ToolCallException('instanceIds must not be empty.');
        }

        $result = $this->client->post('/instances/start', ['instance_ids' => $instanceIds]);

        return array_merge($result, ['message' => count($instanceIds) . ' instance(s) start initiated.']);
    }

    /**
     * Reboot multiple VPS instances simultaneously.
     *
     * @param  array $instanceIds Array of instance IDs (UUIDs) to reboot.
     * @return array<string, mixed> Success confirmation.
     */
    #[McpTool(name: 'reboot_instances')]
    public function rebootInstances(array $instanceIds): array
    {
        if (empty($instanceIds)) {
            throw new ToolCallException('instanceIds must not be empty.');
        }

        $result = $this->client->post('/instances/reboot', ['instance_ids' => $instanceIds]);

        return array_merge($result, ['message' => count($instanceIds) . ' instance(s) reboot initiated.']);
    }

    /**
     * Halt (power off) multiple VPS instances simultaneously.
     *
     * @param  array $instanceIds Array of instance IDs (UUIDs) to halt.
     * @return array<string, mixed> Success confirmation.
     */
    #[McpTool(name: 'halt_instances')]
    public function haltInstances(array $instanceIds): array
    {
        if (empty($instanceIds)) {
            throw new ToolCallException('instanceIds must not be empty.');
        }

        $result = $this->client->post('/instances/halt', ['instance_ids' => $instanceIds]);

        return array_merge($result, ['message' => count($instanceIds) . ' instance(s) halt initiated.']);
    }

    // -------------------------------------------------------------------------
    // Instance metadata / sub-resources
    // -------------------------------------------------------------------------

    /**
     * Get bandwidth usage for a VPS instance.
     *
     * @param  string   $instanceId The instance ID.
     * @param  int|null $dateRange  Number of days to include (default 30, min 1, max 180).
     * @return array<string, mixed>
     */
    #[McpTool(name: 'get_instance_bandwidth')]
    public function getInstanceBandwidth(string $instanceId, ?int $dateRange = null): array
    {
        return $this->client->get("/instances/{$instanceId}/bandwidth", [
            'date_range' => $dateRange,
        ]);
    }

    /**
     * List available upgrades for a VPS instance (plan upgrades).
     *
     * @param  string      $instanceId The instance ID.
     * @param  string|null $type       Upgrade type to filter on (e.g. "all").
     * @return array<string, mixed>
     */
    #[McpTool(name: 'get_instance_upgrades')]
    public function getInstanceUpgrades(string $instanceId, ?string $type = null): array
    {
        return $this->client->get("/instances/{$instanceId}/upgrades", [
            'type' => $type,
        ]);
    }

    /**
     * Get the user data attached to a VPS instance.
     *
     * Returns base64-encoded user data.
     *
     * @param  string $instanceId The instance ID.
     * @return array<string, mixed>
     */
    #[McpTool(name: 'get_instance_user_data')]
    public function getInstanceUserData(string $instanceId): array
    {
        return $this->client->get("/instances/{$instanceId}/user-data");
    }

    /**
     * List the IPv4 addresses assigned to a VPS instance.
     *
     * @param  string   $instanceId The instance ID.
     * @param  int|null $perPage    Number of items per page (default 100, max 500).
     * @param  string|null $cursor  Pagination cursor.
     * @return array<string, mixed>
     */
    #[McpTool(name: 'list_instance_ipv4')]
    public function listInstanceIpv4(string $instanceId, ?int $perPage = null, ?string $cursor = null): array
    {
        return $this->client->get("/instances/{$instanceId}/ipv4", [
            'per_page' => $perPage,
            'cursor'   => $cursor,
        ]);
    }

    /**
     * List the IPv6 addresses assigned to a VPS instance.
     *
     * @param  string $instanceId The instance ID.
     * @return array<string, mixed>
     */
    #[McpTool(name: 'list_instance_ipv6')]
    public function listInstanceIpv6(string $instanceId): array
    {
        return $this->client->get("/instances/{$instanceId}/ipv6");
    }
}
