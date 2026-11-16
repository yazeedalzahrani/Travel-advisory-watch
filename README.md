# Saudi Advisory Watch

Static site plus a scheduled job that refreshes `data.json` from GOV.UK and travel.state.gov every 6 hours.

## Deploy on GitHub Pages (free)
1. Create a new public repository and upload everything in this folder (keep the `.github` folder).
2. Settings > Pages > Source: "Deploy from a branch", branch `main`, folder `/ (root)`.
3. Actions tab > "Update advisories" > Run workflow once to confirm it works.
4. Your site is at https://YOUR-USERNAME.github.io/REPO-NAME/

Netlify or Cloudflare Pages also work: connect the repository, no build command, publish directory `/`.

## What is checked
UK: GOV.UK content API. US: travel.state.gov RSS. France, Italy, Germany: the official advice pages are fetched every run and any new wording is logged (a headless browser is used if a page needs JavaScript). Page text is saved in `snapshots/` to detect changes.

## If a source fails
The page shows "Latest check could not reach ..." and keeps the last good data. Check the Actions log.
