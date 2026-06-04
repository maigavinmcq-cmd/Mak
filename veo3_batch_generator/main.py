try:
    from app.gui import main
except ModuleNotFoundError as exc:
    if exc.name != "PySide6":
        raise
    from app.gui_tk import main


if __name__ == "__main__":
    main()
    