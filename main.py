from calibre.gui2 import error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction
from qt.core import QTimer

try:
    from calibre.utils.localization import get_language as _get_language
except Exception:
    _get_language = None

try:
    from calibre.utils.localization import lang_as_human_readable_name as _lang_human
except Exception:
    _lang_human = None

_COLUMN_LABEL = 'curie'
_COLUMN_FIELD = f'#{_COLUMN_LABEL}'


def _lang_code_to_name(code):
    if not code:
        return 'English'
    for fn in (_get_language, _lang_human):
        if fn:
            try:
                name = fn(code)
                if name and name != code:
                    return name
            except Exception:
                pass
    return code.capitalize()


class CurieAction(InterfaceAction):
    name        = 'Curie'
    action_spec = ('Curie', None, 'Generate character/location data for selected book', None)

    def genesis(self):
        self.qaction.triggered.connect(self.show_dialog)
        QTimer.singleShot(0, self._ensure_custom_column)

    def _ensure_custom_column(self):
        db = self.gui.current_db
        if _COLUMN_FIELD in db.field_metadata:
            return
        try:
            db.new_api.create_custom_column(
                label=_COLUMN_LABEL,
                name='Curie',
                datatype='bool',
                is_multiple=False,
                editable=True,
            )
            info_dialog(
                self.gui, 'Curie',
                'A "Curie" column has been added to your library. '
                'Please restart Calibre for it to appear in the book list.',
                show=True,
            )
        except Exception as exc:
            error_dialog(
                self.gui, 'Curie — column creation failed',
                f'Could not create the "Curie" column automatically.\n\n{exc}',
                show=True,
            )

    def _set_curie(self, book_id, value):
        try:
            db_new = self.gui.current_db.new_api
            if _COLUMN_FIELD in db_new.field_metadata:
                db_new.set_field(_COLUMN_FIELD, {book_id: value})
                self.gui.library_view.model().refresh_ids([book_id])
        except Exception:
            pass

    def show_dialog(self):
        rows = self.gui.library_view.selectionModel().selectedRows()
        if len(rows) != 1:
            error_dialog(self.gui, 'Curie',
                         'Please select exactly one book.', show=True)
            return

        book_id = self.gui.library_view.model().id(rows[0])
        db      = self.gui.current_db.new_api

        if 'EPUB' not in db.formats(book_id):
            error_dialog(self.gui, 'Curie',
                         'The selected book has no EPUB format.', show=True)
            return

        epub_path = db.format_abspath(book_id, 'EPUB')
        mi        = db.get_metadata(book_id)
        authors   = mi.authors or ['Unknown']

        import os
        book_dir = os.path.dirname(epub_path)

        raw_langs = getattr(mi, 'languages', None) or []
        language  = _lang_code_to_name(raw_langs[0]) if raw_langs else 'English'

        from calibre_plugins.curie.ui import CurieDialog
        d = CurieDialog(
            self.gui, book_id, epub_path, book_dir, mi.title, authors, language,
            on_curie=lambda: self._set_curie(book_id, True),
            on_removed=lambda: self._set_curie(book_id, False),
        )
        d.exec_()
