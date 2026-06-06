"""OAuth scopes needed for Apps Script project + deployment management.

These are SEPARATE from runtime scopes a normal MCP install needs.
The user only consents to them when they run the
``setup-apps-script-auto`` CLI; pure-runtime users (who only use
``gdocs_make_tabbed_doc`` + edit tools) never see them.
"""

# https://developers.google.com/apps-script/concepts/scopes
GAS_DEPLOY_SCOPES = [
    # Manage Apps Script projects (create, list, update content).
    "https://www.googleapis.com/auth/script.projects",
    # Create and manage deployments.
    "https://www.googleapis.com/auth/script.deployments",
    # drive.file is also needed: projects.create writes a Drive file
    # (the script project IS a Drive file). google-docs-mcp's runtime
    # already requests drive.file, so this is a no-op for that path;
    # listed here for completeness if anyone extracts gas_deploy.
    "https://www.googleapis.com/auth/drive.file",
]
