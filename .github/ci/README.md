# CI workflow (disabled on initial publish)

`docker-publish.yml.disabled` is the GitHub Actions workflow that builds and
pushes the container images (`ghcr.io/suppakoko/kgaf3-chat`,
`ghcr.io/suppakoko/afmm-smina-mcp`) on each release.

It was committed here (not under `.github/workflows/`) because the initial
push used a token without the `workflow` OAuth scope. To activate it:

```bash
# 1) grant the workflow scope to gh (interactive, one time)
gh auth refresh -h github.com -s workflow
# 2) move it into place and push
git mv .github/ci/docker-publish.yml.disabled .github/workflows/docker-publish.yml
git commit -m "ci: enable docker image publish workflow"
git push
```
