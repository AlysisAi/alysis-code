const REMOTE_OAUTH_REASON =
  "MCP OAuth login is unavailable in a remote VS Code extension host because its loopback callback runs remotely while the authorization browser opens locally. Open the workspace in a local VS Code window to sign in.";

export function isRemoteExtensionHost(remoteName: string | undefined): boolean {
  return typeof remoteName === "string" && remoteName.trim().length > 0;
}

export function mcpOAuthRemoteUnavailableReason(remoteName: string | undefined): string | undefined {
  return isRemoteExtensionHost(remoteName) ? REMOTE_OAUTH_REASON : undefined;
}
