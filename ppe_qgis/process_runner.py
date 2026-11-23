"""Non-blocking native PPE worker; no external runtime or subprocess."""
import os, traceback
from datetime import datetime
from qgis.PyQt.QtCore import QObject, QThread, pyqtSignal, pyqtSlot
from .native_engine import Cancelled, NativePPEEngine

ACTION_NAMES = {"run": "Run", "apply": "Finalize", "zones": "Preparing the crop areas", "export_aoi": "Plot export", "count": "Plant count", "training": "Training data", "labels_create": "Creating labels", "labels_write": "Writing labels", "labels_buffers": "Updating the buffers", "preview": "Opening the image",
                "batch_run": "Batch run", "batch_finalize": "Finalizing the batch"}


def describe_error(exc, action=None, step=None, log_path=None):
    """A message that is never blank: what was being done, the kind of error and its text.
    Some errors carry no text of their own (an empty KeyError, AssertionError or MemoryError),
    which used to show in QGIS as an empty red message."""
    text = str(exc).strip()
    kind = type(exc).__name__
    if isinstance(exc, MemoryError):
        text = "the computer ran out of memory. Close other programs and try again"
    elif isinstance(exc, KeyError):
        text = "missing value {}".format(text or "(no name)")
    what = ACTION_NAMES.get(action) or ("Export" if str(action or "").startswith("export") else "PPE")
    message = "{} stopped{}: {}{}".format(
        what, " while {}".format(step[0].lower() + step[1:]) if step else "",
        "{}: ".format(kind) if kind not in ("ValueError", "RuntimeError") or not text else "",
        text or "no further details")
    if log_path:
        message += "\nDetails are in the log: {}".format(log_path)
    return message

class _Worker(QObject):
    progress=pyqtSignal(int,str); log=pyqtSignal(str); import_ready=pyqtSignal(dict)
    succeeded=pyqtSignal(dict); failed=pyqtSignal(str); finished=pyqtSignal()
    def __init__(self, config, log_path=None): super().__init__(); self.config=dict(config); self.cancelled=False; self.log_path=log_path; self.step=""
    def _progress(self, percent, message): self.step=str(message or ""); self.progress.emit(percent, message)
    @pyqtSlot()
    def run(self):
        try:
            engine=NativePPEEngine(self._progress,self.log.emit,self.import_ready.emit,lambda:self.cancelled)
            action=self.config.get("action")
            if action=="apply": result=engine.apply(self.config)
            elif action=="batch_run": result=engine.batch_run(self.config)
            elif action=="batch_finalize": result=engine.batch_finalize(self.config)
            elif action=="zones": result=engine.prepare_crop_areas(self.config)
            elif action=="export_aoi": result=engine.export_aoi_statistics(self.config)
            elif action=="count":
                from . import plant_count
                result=(plant_count.count_batch if self.config.get("count_items") else plant_count.count_plants)(self.config, self._progress, self.log.emit)
            elif action=="training":
                from . import training_data
                cfg=dict(self.config)
                if cfg.get("batch_manifest"): cfg["items"]=training_data.items_from_batch(cfg["batch_manifest"])
                result=training_data.export_training_tiles(cfg, self._progress, self.log.emit, lambda:self.cancelled)
            elif action in ("labels_create","labels_write","labels_buffers"):
                from . import label_points
                fn={"labels_create":label_points.run_and_create,"labels_write":label_points.write_labels,"labels_buffers":label_points.rebuild_buffers}[action]
                result=fn(self.config, self._progress, self.log.emit, lambda:self.cancelled)
            elif str(action).startswith("export"): result=engine.export_plant_only(self.config)
            else: result=engine.run(self.config)
            self.succeeded.emit(result)
        except Cancelled as exc: self.failed.emit(str(exc).strip() or "Cancelled.")
        except Exception as exc:
            self.log.emit(traceback.format_exc())
            self.failed.emit(describe_error(exc, self.config.get("action"), self.step, self.log_path))
        finally: self.finished.emit()

class PPEProcessRunner(QObject):
    progress=pyqtSignal(int,str); log=pyqtSignal(str); import_ready=pyqtSignal(dict)
    succeeded=pyqtSignal(dict); failed=pyqtSignal(str); running_changed=pyqtSignal(bool)
    def __init__(self,parent=None): super().__init__(parent); self.thread=None; self.worker=None; self._log_lines=[]; self._log_path=None
    @property
    def is_running(self): return self.thread is not None and self.thread.isRunning()
    def start(self,config):
        if self.is_running: raise RuntimeError("PPE is already processing imagery.")
        log_dir=os.path.join(config["output_dir"],"logs"); os.makedirs(log_dir,exist_ok=True)
        self._log_path=os.path.join(log_dir,"ppe_{}.log".format(datetime.now().strftime("%Y%m%d_%H%M%S"))); self._log_lines=[]
        self.thread=QThread(self); self.worker=_Worker(config, self._log_path); self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run); self.worker.progress.connect(self.progress); self.worker.import_ready.connect(self.import_ready)
        self.worker.log.connect(self._capture_log); self.worker.succeeded.connect(self.succeeded); self.worker.failed.connect(self.failed)
        self.worker.finished.connect(self._write_log); self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater); self.thread.finished.connect(self._thread_finished); self.thread.finished.connect(self.thread.deleteLater)
        self.running_changed.emit(True); self._capture_log("Native PPE started; permanent log: {}".format(self._log_path)); self.thread.start()
    def cancel(self):
        if self.worker is not None: self.worker.cancelled=True; self._capture_log("Cancellation requested; finishing the current raster block…")
    def _capture_log(self,message): self._log_lines.append(str(message)); self.log.emit(str(message))
    def _write_log(self):
        try:
            with open(self._log_path,"w",encoding="utf-8") as handle: handle.write("\n".join(self._log_lines)+"\n")
        except OSError: pass
    def _thread_finished(self):
        self.running_changed.emit(False); self.thread=None; self.worker=None


class _PreviewWorker(QObject):
    ready=pyqtSignal(dict); failed=pyqtSignal(str); finished=pyqtSignal()
    def __init__(self, config): super().__init__(); self.config=dict(config)
    @pyqtSlot()
    def run(self):
        try: self.ready.emit(NativePPEEngine().preview(self.config))
        except Exception as exc: self.failed.emit(describe_error(exc, "preview"))
        finally: self.finished.emit()

class PPEPreviewRunner(QObject):
    """Identifies the bands of a newly chosen image in the background so it can be shown on the
    map at once. If the user changes the selection while one request is running, only the
    newest one is answered."""
    ready=pyqtSignal(dict); failed=pyqtSignal(str)
    def __init__(self,parent=None): super().__init__(parent); self.thread=None; self.worker=None; self.pending=None
    @property
    def is_running(self): return self.thread is not None
    def request(self,config):
        if self.thread is not None: self.pending=dict(config); return
        self._start(config)
    def _start(self,config):
        self.thread=QThread(self); self.worker=_PreviewWorker(config); self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run); self.worker.ready.connect(self._ready); self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self.thread.quit); self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._thread_finished); self.thread.finished.connect(self.thread.deleteLater); self.thread.start()
    def _ready(self,result):
        if self.pending is None: self.ready.emit(result)
    def _failed(self,message):
        if self.pending is None: self.failed.emit(message)
    def _thread_finished(self):
        self.thread=None; self.worker=None
        if self.pending is not None:
            config,self.pending=self.pending,None; self._start(config)
