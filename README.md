# schneider-controller-selector

#https://js-forbes.github.io/schneider-controller-selector/

Schneider Electric MP-C Controller Selector. Select point requirements, buffers and eligible controller models. The selector finds a controller combination using built-in I/O only.

Selections are stored in separate SQLite workspaces. Create a workspace to generate a random 8-character code, or enter a code to reconnect to an existing workspace. To choose a code yourself, enter exactly 8 printable Unicode characters without spaces and select **Create custom code**. Codes are case-insensitive and remembered in the current browser. Anyone with a code can read, change, or delete selections in that workspace, so share it only with people you trust. The server stores a hash of each code and limits repeated invalid-code attempts. This provides lightweight shared access, not account-level security.

The first workspace created copies selections from the earlier unpartitioned SQLite table. When creating a new workspace, existing browser-local selections are also copied into it. Use **Export database** and **Import database** to back up or transfer selections as a portable JSON file.
