"""PyInstaller entry point of the console tool (NorrinTPM-cli.exe): same commands as `python -m tpm`."""
import multiprocessing

from tpm.desktop import prepare_environment

if __name__ == "__main__":
    multiprocessing.freeze_support()
    prepare_environment()  # same data folder, settings and keys as the windowed app
    from tpm.cli import main

    raise SystemExit(main())
