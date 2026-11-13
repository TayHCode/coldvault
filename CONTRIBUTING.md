# Contributing

Keep the project small and dependency-free. Run the offline tests with `python3 -m unittest discover -s tests`. Use synthetic transcript fixtures only.

Finish the first version before adding features. Its 12 required capabilities are a vault template, a documented install, guided setup, automatic transcript import, GitHub documentation sync, reliable incremental sync, duplicate prevention, safe handling of existing files, scheduled sync, a status command, docs for adding sources and customizing, and uninstall and recovery steps. Until all 12 work, later possibilities wait: new transcript providers, Notion as a destination, smarter linking, AI summaries, semantic search, new import or export formats, ColdStack integrations and a configuration UI. Fixes to the 12 come first.

Report bugs and propose changes at https://github.com/TayHCode/coldvault/issues. For security issues, follow [SECURITY.md](SECURITY.md).

Before each release, run the fresh-machine acceptance test as root in a new, throwaway Ubuntu VM with the release candidate: `sudo bash tests/acceptance.sh coldvault.py`. It follows README.md as a new user, from download to a scheduled run and `status`, and fails if that takes 10 minutes or more. After the release, run it without the argument to check the README download too.
