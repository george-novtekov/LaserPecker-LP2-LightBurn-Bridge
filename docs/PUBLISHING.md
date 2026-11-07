# Maintain the clean GitHub project

Use this project folder as the repository root. The older `LaserPecker` folder is
the research workspace and is not the source to upload. Build output is distributed
as **GitHub Release assets**, so app users do not need Python or a source checkout.

## First source upload

If source has already been published, clone the repository instead of repeating
initialization. Otherwise open a terminal in this clean project folder:

```powershell
git init -b main
git add .
git status --short
git diff --cached --stat
```

Review the staged list: runtime, tests/fixtures, guides, launchers, and repository
configuration. `.venv`, `dist`, `build`, executables, and private `LP2BridgeData`
are ignored. The root contains no selected project license yet; add the license
you want to use before inviting reuse under it.

If Git asks for identity, configure your preferred name and GitHub commit email
locally in this repository. Then:

```powershell
git commit -m "Add clean LP2 LightBurn bridge project"
git remote add origin https://github.com/george-novtekov/LaserPecker-LP2-LightBurn-Bridge.git
git push -u origin main
```

If the remote gained an initial commit, fetch and inspect it before integrating.
Do not force-push over existing work.

GitHub's **Add file → Upload files** is an alternative for the first upload:
upload the clean source folders/files, keeping their relative paths. Exclude the
ignored generated folders. Git CLI is preferable for later maintenance.

## Publish the Windows download

1. Follow [Development](DEVELOPMENT.md) to test, build, smoke-test, and package.
2. Commit and push the exact source used by the build.
3. In the repository, open **Releases → Draft a new release**.
4. Select/create tag **`scan-8`** at the intended source commit (use a new tag for
   each later release). Set the title to **LP2 LightBurn Bridge scan-8 — Windows**.
5. Paste the relevant text from [release notes](../RELEASE-NOTES.md).
6. Attach both files from `dist/`:
   - `LP2-LightBurn-Bridge-Windows.zip`
   - `LP2-LightBurn-Bridge-Windows.zip.sha256.txt`
7. Set it as the latest release when appropriate, publish, then open the public
   release page and verify both assets are present and download correctly.

The README's download button uses `/releases/latest`, so it continues to work
across versions. Users should download the named Windows ZIP under **Assets**,
not GitHub's automatic Source code archives.

If GitHub CLI is available and authenticated, the equivalent release command is:

```powershell
gh release create scan-8 --target main --title "LP2 LightBurn Bridge scan-8 - Windows" --notes-file RELEASE-NOTES.md .\dist\LP2-LightBurn-Bridge-Windows.zip .\dist\LP2-LightBurn-Bridge-Windows.zip.sha256.txt
```

## Later changes

Edit only the clean source, run the offline tests, and review `git diff`. For a
new binary, update version strings/release notes, build again, check the frozen
self-test and disconnected GUI, then publish source and matching release assets.
Do not publish runtime captures, personal calibration, vendor binaries, or old
research bundles.
