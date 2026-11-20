# Remote GitHub deployment and encrypted nightly backups

This is a **manual-approval** workflow. Pushing code to \`main\` never updates the live printer server automatically.

## Overview

- **Approve an update:** after code reaches \`main\` and the Windows CI test suite passes for that exact commit, open **Actions → Approve LocalPrintServer deployment → Run workflow**. The workflow writes an approved commit SHA to the \`deployment-requests\` branch. It does not execute application code on GitHub's runner.
- **Apply the request:** the Windows PC checks the request every five minutes. It pulls the exact approved commit, runs the existing installer, checks \`manage.py check\` and \`http://127.0.0.1:5000/login\`, and records the result. If installation or health checks fail, it attempts rollback from the pre-update local backup.
- **Nightly backup:** at 02:00 local time, and after startup if a scheduled run was missed, the PC makes a SQLite online-backup snapshot, copies the data tree, checks that referenced uploads and active-job files are present, creates SHA-256 inventory metadata, encrypts the archive, and uploads it as an asset in a dated GitHub Release in \`<owner>/<private-backup-repo>\`.
- **Retention:** the newest 30 backup releases are kept by default. Old releases and their backup tags are removed after a new backup has been uploaded and verified.
- **No inbound port:** the PC initiates all GitHub connections. Do not port-forward the LocalPrintServer web port.

## Prerequisites

The live service is expected to be installed at \`C:\\LocalPrintServer\`, use the existing \`LocalPrintServer\` NSSM service, and use the Python 3.13 interpreter in the deployment configuration. Setup validates the running service and login endpoint before registering the tasks.

### 1. Create two fine-grained GitHub tokens

Create tokens in [GitHub fine-grained personal access tokens](https://github.com/settings/personal-access-tokens/new). Select **Only select repositories** for each token.

**Token A — source read-only**
- Repository: \`<owner>/<private-source-repo>\`
- Repository permission: **Contents — Read-only**
- Repository permission: **Actions — Read-only** (the updater verifies the manual approval run directly with GitHub before touching production).

**Token B — backup writer**
- Repository: \`<owner>/<private-backup-repo>\`
- Repository permission: **Contents — Read and write**

Keep the tokens separate. Do not paste them into source files, a Git remote URL, an issue, or a commit. If a token expires or is revoked, scheduled operations will fail and record an error in the local operations log.

### 2. Set the backup encryption passphrase

Choose a strong passphrase with at least 15 characters and keep a copy **offline and separate from this PC**. The setup script stores it encrypted with Windows DPAPI for unattended nightly tasks; that local protected copy cannot replace the offline copy if the PC is lost or reinstalled.

The backup archive uses AES-256-CBC encryption with an HMAC-SHA256 integrity check and PBKDF2-derived keys. The database, uploads, session-signing key, configuration, and associated file tree are inside the encrypted archive; they are not committed as plaintext repository files.

### 3. Install the scheduled maintenance tasks

After these changes have been merged to \`main\`, download the source repository ZIP from GitHub and extract it into a **separate installation/setup folder** (not over \`C:\\LocalPrintServer\`). Open PowerShell **as Administrator** in the extracted source root and run:

\`\`\`powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\setup_remote_management.ps1
\`\`\`

Enter Token A, Token B, and the encryption passphrase when prompted. The setup script:
- verifies that both repositories are private and the supplied tokens can read the expected files;
- stores credentials protected with Windows DPAPI and restricts the operations folder ACL to SYSTEM and Administrators;
- registers a five-minute approval poll and a 02:00 daily backup task running as SYSTEM;
- does **not** stop, restart, or replace the live print service.

The first backup upload is intentionally a separate action, so it is easy to check the permissions and output. Start it from an elevated PowerShell:

\`\`\`powershell
Start-ScheduledTask -TaskName "LocalPrintServer-Nightly-GitHub-Backup"
Start-Sleep -Seconds 10
Get-ScheduledTaskInfo -TaskName "LocalPrintServer-Nightly-GitHub-Backup"
Get-Content "C:\\ProgramData\\LocalPrintServerOps\\operations.log" -Tail 40
\`\`\`

Wait for the backup task to finish; the GitHub upload may take longer than ten seconds for large uploads. Verify that a dated release with an uploaded \`.lpsenc\` asset appears in the private backup repository's Releases page.

## Updating the live server

1. Commit and push the desired application changes to \`main\`.
2. Wait until **Automated tests** succeeds for the latest \`main\` commit.
3. Open `https://github.com/<owner>/<private-source-repo>/actions`.
4. Select **Approve LocalPrintServer deployment**, click **Run workflow**, and confirm **Run workflow**.
5. The PC sees the request on its next scheduled poll, normally within five minutes.

**Safety gate:** the updater refuses to replace application files unless `last-backup.json` records a successfully uploaded and verified encrypted GitHub backup completed within the previous 36 hours. Run the backup task successfully once after setup and confirm its dated release asset exists before the first deployment. If the nightly backup stops succeeding or becomes stale, updates are blocked until a fresh verified backup succeeds.

The button approves the latest default-branch commit that passed CI at the moment the workflow runs. If tests have not passed for that exact SHA, no deployment request is created. A failed request is not retried endlessly; click **Run workflow** again to create a new request after resolving the problem.

Local state and logs are stored under \`C:\\ProgramData\\LocalPrintServerOps\`. Installer backups remain under \`C:\\LocalPrintServer_backups\`. A failed rollout is logged and triggers an attempted restore using the rollback script from the new local backup. Automatic rollback still needs to be verified on the actual PC before relying on unattended releases.

## Restoring a GitHub backup

1. Open the private [backup repository](https://github.com/<owner>/<private-backup-repo>/releases) and download the desired dated \`.lpsenc\` asset.
2. Download \`scripts/remote_ops/decrypt_backup.ps1\` and \`scripts/remote_ops/ops-common.ps1\` from the source repository into the same folder.
3. In PowerShell, run:

\`\`\`powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\decrypt_backup.ps1 -InputFile .\\LocalPrintServer-YYYYMMDD-HHMMSS.lpsenc -ExtractAndVerify
\`\`\`

Enter the offline backup passphrase when prompted. The script authenticates before decrypting and verifies the extracted files against \`SHA256SUMS.json\`. It creates a verification folder next to the ZIP; this step **does not replace the running server automatically**. Use the included \`RESTORE.txt\` and \`metadata.json\` to restore manually, keep the current files until verification is complete, and test the recovery on a separate location before an actual disaster recovery.

## Operational limitations

- This code has to be merged and the Windows setup script run before GitHub's button or nightly task can affect the PC. Creating workflows in a branch alone does not enable them on \`main\`.
- GitHub token access, Actions permissions, and repository privacy must remain valid.
- The backup captures a consistent SQLite snapshot and checks current upload and active-job file references, but it cannot guarantee a business-consistent image during sustained file changes; if consistency checks fail after retries, the backup is rejected rather than uploaded.
- Nightly encrypted backup releases protect confidentiality in GitHub, but keep the passphrase separately and periodically test decryption and restore.
- A passing HTTP check does not replace on-site printer acceptance tests. The workflow does not send a test page or submit a print job automatically.
