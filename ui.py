import json
import math
import os
from datetime import datetime

from calibre.gui2 import error_dialog, question_dialog  # question_dialog used in _remove
from qt.core import (
    QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QCheckBox, QComboBox, QMessageBox, QPushButton,
    QProgressBar, QSizePolicy, QSpinBox, Qt, QVBoxLayout, QWidget, pyqtSlot,
)

from calibre_plugins.curie.api_client import (
    MODELS, OLLAMA_OVERHEAD_TOKENS, calc_cost, list_ollama_models,
)
from calibre_plugins.curie.config import prefs
from calibre_plugins.curie.worker import CurieWorker


class CurieDialog(QDialog):

    def __init__(self, parent, book_id, epub_path, book_dir, title, authors, language='English',
                 on_curie=None, on_removed=None):
        super().__init__(parent)
        self.book_id           = book_id
        self.epub_path         = epub_path
        self.book_dir          = book_dir
        self.output_path       = os.path.join(book_dir, 'curie.json')
        self.book_title        = title
        self.book_author       = authors[0] if authors else 'Unknown'
        self.book_language     = language
        self.worker            = None
        self.epub_chars        = None  # populated after UI is built
        self._on_curie   = on_curie
        self._on_removed       = on_removed

        self.setWindowTitle(f'Curie — {title}')
        self.setMinimumWidth(500)
        self._build_ui()
        self._load_prefs()
        self.epub_chars = self._count_epub_chars()
        self._update_estimate()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.finished.connect(self.close)  # QThread built-in — fires when run() exits
            self.run_btn.setEnabled(False)
            self.readd_btn.setEnabled(False)
            self.status_label.setVisible(True)
            self.status_label.setText('Cancelling…')
            event.ignore()
            return
        event.accept()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ── Configuration ─────────────────────────────────────────────────────
        cfg_box = QGroupBox('Configuration')
        cfg_lay = QVBoxLayout(cfg_box)

        # Provider row
        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel('Provider'))
        self.provider_combo = QComboBox()
        self.provider_combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.provider_combo.addItem('Anthropic (Cloud)', 'anthropic')
        self.provider_combo.addItem('Ollama (Local LLM)', 'ollama')
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        provider_row.addWidget(self.provider_combo)
        provider_row.addStretch()
        cfg_lay.addLayout(provider_row)

        # Anthropic section
        self.anthropic_section = QWidget()
        anthropic_form = QFormLayout(self.anthropic_section)
        anthropic_form.setContentsMargins(0, 0, 0, 0)
        anthropic_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText('sk-ant-…')
        anthropic_form.addRow('Claude API Key', self.api_key_input)

        self.model_combo = QComboBox()
        self.model_combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for model_id, info in MODELS.items():
            self.model_combo.addItem(info['label'], model_id)
        self.model_combo.currentIndexChanged.connect(self._update_estimate)
        anthropic_form.addRow('Model', self.model_combo)

        cfg_lay.addWidget(self.anthropic_section)

        # Ollama section
        self.ollama_section = QWidget()
        ollama_form = QFormLayout(self.ollama_section)
        ollama_form.setContentsMargins(0, 0, 0, 0)
        ollama_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self.ollama_host_input = QLineEdit()
        self.ollama_host_input.setPlaceholderText('http://localhost:11434')
        self.ollama_host_input.textChanged.connect(self._update_estimate)
        ollama_form.addRow('Ollama Host', self.ollama_host_input)

        ollama_model_row = QHBoxLayout()
        self.ollama_model_combo = QComboBox()
        self.ollama_model_combo.setEditable(True)
        self.ollama_model_combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.ollama_model_combo.currentTextChanged.connect(self._update_estimate)
        ollama_model_row.addWidget(self.ollama_model_combo)
        refresh_btn = QPushButton('↻')
        refresh_btn.setFixedWidth(32)
        refresh_btn.setToolTip('Refresh model list from Ollama')
        refresh_btn.clicked.connect(self._refresh_ollama_models)
        ollama_model_row.addWidget(refresh_btn)
        ollama_form.addRow('Model', ollama_model_row)

        self.ollama_ctx_spin = QSpinBox()
        self.ollama_ctx_spin.setRange(2048, 131072)
        self.ollama_ctx_spin.setSingleStep(1024)
        self.ollama_ctx_spin.setValue(8192)
        self.ollama_ctx_spin.setSuffix(' tokens')
        self.ollama_ctx_spin.setToolTip(
            'Context window size of your Ollama model.\n'
            'For a 7B 4-bit model on a 3080 10 GB, 8192 is safe.\n'
            'Increase if your model supports a larger context.'
        )
        self.ollama_ctx_spin.valueChanged.connect(self._update_estimate)
        ollama_form.addRow('Context Size', self.ollama_ctx_spin)

        cfg_lay.addWidget(self.ollama_section)

        # Common rows (always visible)
        common_form = QFormLayout()
        common_form.setContentsMargins(0, 0, 0, 0)
        common_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        include_row = QHBoxLayout()
        self.char_check  = QCheckBox('Characters')
        self.place_check = QCheckBox('Places')
        self.char_check.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.place_check.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.char_check.stateChanged.connect(self._update_estimate)
        self.place_check.stateChanged.connect(self._update_estimate)
        include_row.addWidget(self.char_check)
        include_row.addWidget(self.place_check)
        include_row.addStretch()
        common_form.addRow('Include', include_row)

        self.language_label = QLabel()
        lang_field_label = QLabel('Book Lang.')
        lang_field_label.setToolTip('Fetched from the book\'s metadata.')
        common_form.addRow(lang_field_label, self.language_label)

        cfg_lay.addLayout(common_form)
        root.addWidget(cfg_box)

        # Formatting Options
        fmt_box  = QGroupBox('Formatting Options')
        fmt_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        fmt_lay  = QVBoxLayout(fmt_box)
        fmt_note = QLabel('<small style="color: gray;">These options do not affect the Claude output. Changing these and updating Hints is only processed locally.</small>')
        fmt_note.setTextFormat(Qt.TextFormat.RichText)
        fmt_lay.addWidget(fmt_note)
        fmt_form = QFormLayout()
        fmt_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        fmt_form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self.reader_combo = QComboBox()
        self.reader_combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.reader_combo.addItem('KOReader', 'koreader')
        self.reader_combo.addItem('Nickel (Kobo)', 'nickel')
        fmt_form.addRow('Target Reader', self.reader_combo)

        self.density_combo = QComboBox()
        self.density_combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.density_combo.addItem('At every mention', 'every_mention')
        self.density_combo.addItem('Once every 10 paragraphs', 'every_10_paragraphs')
        self.density_combo.addItem('Once per chapter', 'once_per_chapter')
        fmt_form.addRow('Hint Density', self.density_combo)

        fmt_lay.addLayout(fmt_form)
        root.addWidget(fmt_box)

        # Analyze group
        est_box = QGroupBox('Process Book')
        est_lay = QVBoxLayout(est_box)
        est_cost_heading = QLabel('<small><b>Est. Analysis Cost</b></small>')
        est_cost_heading.setTextFormat(Qt.TextFormat.RichText)
        est_lay.addWidget(est_cost_heading)
        self.estimate_label = QLabel()
        self.estimate_label.setWordWrap(True)
        self.estimate_label.setTextFormat(Qt.TextFormat.PlainText)
        est_lay.addWidget(self.estimate_label)
        btn_row = QHBoxLayout()
        self.readd_btn = QPushButton('Update Hints in book')
        self.readd_btn.clicked.connect(self._readd)
        btn_row.addWidget(self.readd_btn)
        self.run_btn = QPushButton('Analyze && Add Hints')
        self.run_btn.setDefault(True)
        self.run_btn.clicked.connect(self._run)
        btn_row.addWidget(self.run_btn)
        est_lay.addLayout(btn_row)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)   # indeterminate spinner
        self.progress_bar.setVisible(False)
        est_lay.addWidget(self.progress_bar)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        est_lay.addWidget(self.status_label)
        root.addWidget(est_box)

        # Book Status
        current_box = QGroupBox('Book Status')
        current_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        current_lay = QVBoxLayout(current_box)
        self.current_status_label = QLabel()
        self.current_status_label.setWordWrap(True)
        self.current_status_label.setTextFormat(Qt.TextFormat.RichText)
        current_lay.addWidget(self.current_status_label)
        self.remove_btn = QPushButton('Remove Hints from Book')
        self.remove_btn.clicked.connect(self._remove)
        current_lay.addWidget(self.remove_btn)
        root.addWidget(current_box)

        # Close
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        root.addWidget(buttons)

    # ── Preferences ───────────────────────────────────────────────────────────

    def _load_prefs(self):
        # Provider
        provider_id = prefs['provider']
        for i in range(self.provider_combo.count()):
            if self.provider_combo.itemData(i) == provider_id:
                self.provider_combo.setCurrentIndex(i)
                break

        # Anthropic
        self.api_key_input.setText(prefs['api_key'])
        model_id = prefs['model']
        for i in range(self.model_combo.count()):
            if self.model_combo.itemData(i) == model_id:
                self.model_combo.setCurrentIndex(i)
                break

        # Ollama
        self.ollama_host_input.setText(prefs['ollama_host'])
        self.ollama_ctx_spin.setValue(prefs['ollama_context_size'])
        self._refresh_ollama_models()
        saved_ollama_model = prefs['ollama_model']
        if saved_ollama_model:
            self.ollama_model_combo.setEditText(saved_ollama_model)

        # Common
        self.char_check.setChecked(prefs['include_characters'])
        self.place_check.setChecked(prefs['include_places'])
        self.language_label.setText(self.book_language)

        reader_id = prefs['target_reader']
        for i in range(self.reader_combo.count()):
            if self.reader_combo.itemData(i) == reader_id:
                self.reader_combo.setCurrentIndex(i)
                break

        density_id = prefs['hint_density']
        for i in range(self.density_combo.count()):
            if self.density_combo.itemData(i) == density_id:
                self.density_combo.setCurrentIndex(i)
                break

        self._on_provider_changed()
        self._update_current_status()

    def _update_current_status(self):
        if not self._is_enriched():
            self.current_status_label.setText('No hints added.')
            self.remove_btn.setEnabled(False)
            self._update_buttons()
            return
        self.remove_btn.setEnabled(True)
        try:
            with open(self.output_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            n_chars = len(data.get('characters', []))
            n_locs  = len(data.get('locations', []))
            parts = []
            if n_chars:
                parts.append(f'{n_chars} character{"s" if n_chars != 1 else ""}')
            if n_locs:
                parts.append(f'{n_locs} location{"s" if n_locs != 1 else ""}')
            line1 = ', '.join(parts) if parts else 'Hints injected.'
            meta  = data.get('_curie_meta')
            if meta:
                refs     = meta.get('refs_injected', 0)
                chapters = meta.get('chapters_modified', 0)
                cost     = meta.get('total_cost', 0.0)
                line2 = f'Injected {refs} hints across {chapters} chapters. Cost: ${cost:.2f}.'
                self.current_status_label.setText(
                    f'{line1}<br><small style="color: gray;">{line2}</small>'
                )
            else:
                self.current_status_label.setText(line1)
        except Exception:
            self.current_status_label.setText('Hints injected.')
        self._update_buttons()

    def _update_buttons(self):
        has_hints = self._is_enriched() and os.path.exists(self.output_path)
        self.readd_btn.setVisible(has_hints)
        self.run_btn.setText('Generate Hints again' if has_hints else 'Analyze && Add Hints')

    def _is_enriched(self):
        try:
            import zipfile as _zf, re as _re
            pat = _re.compile(rb'data-curie="true"')
            with _zf.ZipFile(self.epub_path, 'r') as zf:
                for name in zf.namelist():
                    if name.lower().endswith(('.html', '.xhtml', '.htm')):
                        if pat.search(zf.read(name)):
                            return True
        except Exception:
            pass
        return False

    def _save_prefs(self):
        prefs['provider']            = self.provider_combo.currentData()
        prefs['api_key']             = self.api_key_input.text().strip()
        prefs['model']               = self.model_combo.currentData()
        prefs['include_characters']  = self.char_check.isChecked()
        prefs['include_places']      = self.place_check.isChecked()
        prefs['target_reader']       = self.reader_combo.currentData()
        prefs['hint_density']        = self.density_combo.currentData()
        prefs['ollama_host']         = self.ollama_host_input.text().strip()
        prefs['ollama_model']        = self.ollama_model_combo.currentText().strip()
        prefs['ollama_context_size'] = self.ollama_ctx_spin.value()

    # ── Cost estimate ─────────────────────────────────────────────────────────

    def _count_epub_chars(self):
        try:
            from calibre_plugins.curie.epub_utils import extract_spine_texts
            chapters = extract_spine_texts(self.epub_path)
            return sum(len(text) for _, text in chapters)
        except Exception:
            return None

    def _describe_existing(self, path):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            chars = len(data.get('characters', []))
            locs  = len(data.get('locations', []))
            return (
                f'Created: {mtime.strftime("%Y-%m-%d %H:%M")}<br>'
                f'Contains: {chars} characters, {locs} locations'
            )
        except Exception:
            return 'Could not read existing file.'

    def _update_estimate(self):
        is_ollama  = self.provider_combo.currentData() == 'ollama'
        has_chars  = self.char_check.isChecked()
        has_places = self.place_check.isChecked()

        if not has_chars and not has_places:
            self.estimate_label.setText('Select at least Characters or Places.')
            return

        if is_ollama:
            lines = ['Step 1 (local model research):  Free']
            if self.epub_chars is not None:
                ctx_size     = self.ollama_ctx_spin.value()
                chunk_budget = max(500, ctx_size - OLLAMA_OVERHEAD_TOKENS)
                n_chunks     = max(1, math.ceil(self.epub_chars / (chunk_budget * 4)))
                passes       = f'  ({n_chunks} {"pass" if n_chunks == 1 else "passes"})'
                lines.append(f'Step 2 (EPUB analysis):  Free{passes}')
            else:
                lines.append('Step 2 (EPUB analysis):  Free')
            lines.append('Total:  Free (local processing)')
            self.estimate_label.setText('\n'.join(lines))
            return

        # Anthropic pricing
        model_id   = self.model_combo.currentData()
        info       = MODELS.get(model_id, {})
        scale      = 1.0 if (has_chars and has_places) else 0.65
        s1_lo, s1_hi = info.get('estimate_step1', (0, 0))

        lines = [
            f'Step 1 (web research)  ~${s1_lo * scale:.2f} – ${s1_hi * scale:.2f}'
        ]

        if self.epub_chars is not None:
            from calibre_plugins.curie.epub_utils import CHUNK_TOKEN_BUDGET
            n_chunks        = max(1, math.ceil(self.epub_chars / (CHUNK_TOKEN_BUDGET * 4)))
            cache_write_tok = self.epub_chars / 4
            input_tok       = 1_500 * n_chunks
            output_tok_lo   = 1_500 * n_chunks
            output_tok_hi   = 4_000 * n_chunks

            p = info
            def _step2_cost(out_tok):
                return (
                    cache_write_tok * p['cache_write'] +
                    input_tok       * p['input'] +
                    out_tok         * p['output']
                ) / 1_000_000 * scale

            cost_lo = _step2_cost(output_tok_lo)
            cost_hi = _step2_cost(output_tok_hi)

            passes = f'  ({n_chunks} passes)' if n_chunks > 1 else ''
            lines.append(
                f'Step 2 (EPUB analysis)  ~${cost_lo:.2f} – ${cost_hi:.2f}{passes}'
            )
            lines.append(
                f'Total  ~${s1_lo * scale + cost_lo:.2f} – ${s1_hi * scale + cost_hi:.2f}'
            )
        else:
            s2_lo, s2_hi = info.get('estimate_step2', (0, 0))
            lines.append(
                f'Step 2 (EPUB analysis)  ~${s2_lo * scale:.2f} – ${s2_hi * scale:.2f}'
            )
            lines.append(
                f'Total  ~${(s1_lo + s2_lo) * scale:.2f} – ${(s1_hi + s2_hi) * scale:.2f}'
            )

        self.estimate_label.setText('\n'.join(lines))

    # ── Run ───────────────────────────────────────────────────────────────────

    def _run(self):
        is_ollama = self.provider_combo.currentData() == 'ollama'

        if is_ollama:
            if not self.ollama_model_combo.currentText().strip():
                error_dialog(self, 'Curie', 'Please enter an Ollama model name.', show=True)
                return
        else:
            if not self.api_key_input.text().strip():
                error_dialog(self, 'Curie', 'Please enter your Anthropic API key.', show=True)
                return

        if not self.char_check.isChecked() and not self.place_check.isChecked():
            error_dialog(self, 'Curie',
                         'Select at least one of Characters or Places.', show=True)
            return

        self._save_prefs()

        msg = QMessageBox(self)
        msg.setWindowTitle('Confirm Analysis')
        if is_ollama:
            model_name = self.ollama_model_combo.currentText().strip()
            msg.setText(
                f'This will process the book using local Ollama model "{model_name}".\n\n'
                f'{self.estimate_label.text()}\n\n'
                f'Proceed?'
            )
        else:
            msg.setText(
                f'This will use the Claude API.\n\n'
                f'{self.estimate_label.text()}\n\n'
                f'Proceed?'
            )
        btn_proceed = msg.addButton('Proceed', QMessageBox.ButtonRole.AcceptRole)
        btn_cancel  = msg.addButton('Cancel',  QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(btn_cancel)
        msg.exec_()
        if msg.clickedButton() is not btn_proceed:
            return

        self._start_worker(inject_only=False)

    def _readd(self):
        if not os.path.exists(self.output_path):
            error_dialog(self, 'Curie', 'No existing analysis found for this book.', show=True)
            return
        self._save_prefs()
        self._start_worker(inject_only=True)

    def _start_worker(self, inject_only):
        self.run_btn.setEnabled(False)
        self.readd_btn.setEnabled(False)
        self.remove_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.status_label.setVisible(True)
        self.status_label.setText('Starting…')

        self.worker = CurieWorker(
            api_key             = self.api_key_input.text().strip(),
            title               = self.book_title,
            author              = self.book_author,
            epub_path           = self.epub_path,
            include_characters  = self.char_check.isChecked(),
            include_places      = self.place_check.isChecked(),
            language            = self.book_language,
            model               = self.model_combo.currentData(),
            output_path         = self.output_path,
            inject_only         = inject_only,
            target_reader       = self.reader_combo.currentData(),
            hint_density        = self.density_combo.currentData(),
            provider            = self.provider_combo.currentData(),
            ollama_host         = self.ollama_host_input.text().strip() or 'http://localhost:11434',
            ollama_model        = self.ollama_model_combo.currentText().strip(),
            ollama_context_size = self.ollama_ctx_spin.value(),
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.step1_done.connect(self._on_step1_done)
        self.worker.analysis_done.connect(self._on_finished)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def _on_provider_changed(self):
        is_ollama = self.provider_combo.currentData() == 'ollama'
        self.anthropic_section.setVisible(not is_ollama)
        self.ollama_section.setVisible(is_ollama)
        self._update_estimate()

    def _refresh_ollama_models(self):
        host    = self.ollama_host_input.text().strip() or 'http://localhost:11434'
        models  = list_ollama_models(host)
        current = self.ollama_model_combo.currentText()
        self.ollama_model_combo.clear()
        for m in models:
            self.ollama_model_combo.addItem(m)
        if current:
            self.ollama_model_combo.setEditText(current)

    def _remove(self):
        msg = QMessageBox(self)
        msg.setWindowTitle('Remove Hints from Book')
        msg.setText(
            'This will remove all Curie footnote hints from the EPUB. '
            'The generated hint data is still saved inside the book\'s folder.<br><br>'
            'Do you want to continue?'
        )
        btn_remove = msg.addButton('Remove', QMessageBox.ButtonRole.AcceptRole)
        btn_cancel = msg.addButton('Cancel', QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(btn_cancel)
        msg.exec_()
        if msg.clickedButton() is not btn_remove:
            return
        try:
            from calibre_plugins.curie.epub_injector import remove_injections
            result  = remove_injections(self.epub_path)
            cleaned = result.get('chapters_cleaned', 0)

            self.status_label.setVisible(False)
            self._update_current_status()  # also calls _update_buttons
            if self._on_removed:
                self._on_removed()
        except Exception as exc:
            error_dialog(self, 'Curie Error', str(exc), show=True)

    # ── Slots ─────────────────────────────────────────────────────────────────

    @pyqtSlot(str)
    def _on_progress(self, msg):
        self.status_label.setText(msg)

    @pyqtSlot(dict, dict)
    def _on_step1_done(self, book_data, usage1):
        pass

    @pyqtSlot(dict, dict, dict, str, dict)
    def _on_finished(self, book_data, usage1, usage2, output_path, inject_stats):
        self.progress_bar.setVisible(False)
        self.status_label.setVisible(False)
        self.run_btn.setEnabled(True)
        self.readd_btn.setEnabled(True)

        # Persist meta into the JSON so status survives dialog close/reopen
        model_id   = self.model_combo.currentData()
        total_cost = calc_cost(usage1, model_id) + calc_cost(usage2, model_id)
        meta = {
            'refs_injected':     inject_stats.get('refs_injected', 0),
            'chapters_modified': inject_stats.get('chapters_modified', 0),
            'total_cost':        total_cost,
        }
        try:
            with open(output_path, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            saved['_curie_meta'] = meta
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(saved, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

        self._update_current_status()
        if self._on_curie:
            self._on_curie()

    @pyqtSlot(str)
    def _on_error(self, msg):
        self.progress_bar.setVisible(False)
        self.run_btn.setEnabled(True)
        self.readd_btn.setEnabled(True)
        self._update_current_status()
        self.status_label.setText('An error occurred.')
        error_dialog(self, 'Curie Error', msg, show=True)

