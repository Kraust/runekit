import io
import logging
import time

import Quartz
import objc
from PIL import Image
from PySide2.QtCore import QRect
from PySide2.QtGui import QGuiApplication
import ApplicationServices

from ..instance import GameInstance

_debug_dump_file = False
logger = logging.getLogger(__name__)


def cgrectref_to_qrect(cgrectref) -> QRect:
    _, cgrect = Quartz.CGRectMakeWithDictionaryRepresentation(cgrectref, None)
    return QRect(
        cgrect.origin.x, cgrect.origin.y, cgrect.size.width, cgrect.size.height
    )


def cgimageref_to_image(imgref) -> Image:
    buf = Quartz.CFDataCreateMutable(None, 0)

    dest = Quartz.CGImageDestinationCreateWithData(buf, "public.tiff", 1, None)
    Quartz.CGImageDestinationAddImage(dest, imgref, None)
    Quartz.CGImageDestinationFinalize(dest)

    buf_size = Quartz.CFDataGetLength(buf)
    py_buf = io.BytesIO()
    py_buf.write(Quartz.CFDataGetBytePtr(buf).as_buffer(buf_size))
    py_buf.seek(0)

    out = Image.open(py_buf, formats=("TIFF",))

    if _debug_dump_file:
        out.save("/tmp/game.bmp")
        open("/tmp/native.xbm", "wb").write(py_buf.getbuffer())

    return out


# This decorator does not support being a class method
@objc.callbackFor(ApplicationServices.AXObserverCreate)
def on_ax_event(observer, element, notification, ptr):
    try:
        self: "QuartzGameInstance" = objc.context.get(ptr)
    except KeyError:
        logger.warning(
            "Received AX event callback for missing pointer %d, removing", ptr
        )
        ApplicationServices.AXObserverRemoveNotification(
            observer, element, notification
        )
        return

    if notification == ApplicationServices.kAXApplicationActivatedNotification:
        self._is_active = True
        self.activeChanged.emit(True)
    elif notification == ApplicationServices.kAXApplicationDeactivatedNotification:
        self._is_active = False
        self.activeChanged.emit(False)
    elif notification == ApplicationServices.kAXWindowResizedNotification:
        self.positionChanged.emit(self.get_position())
    elif notification == ApplicationServices.kAXWindowMovedNotification:
        self.positionChanged.emit(self.get_position())
    else:
        self.logger.warning("Got unknown AX event %s", notification)


class QuartzGameInstance(GameInstance):
    _is_active = False

    __game_last_grab = 0.0
    __game_last_image = None

    def __init__(self, manager, wid, pid, **kwargs):
        super().__init__(**kwargs)
        self.manager = manager
        self.wid = wid
        self.pid = pid
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}:{pid}")
        self.obj_pointer = objc.context.register(self)

        self._setup_observer()
        self.update_is_active()

    def _setup_observer(self):
        self._ax_element = ApplicationServices.AXUIElementCreateApplication(self.pid)
        self._ax_observed = [
            ApplicationServices.kAXApplicationActivatedNotification,
            ApplicationServices.kAXApplicationDeactivatedNotification,
            ApplicationServices.kAXWindowResizedNotification,
            ApplicationServices.kAXWindowMovedNotification,
        ]
        err, self._observer = ApplicationServices.AXObserverCreate(
            self.pid, on_ax_event, None
        )
        if err != ApplicationServices.kAXErrorSuccess:
            raise AXAPIError(err)

        for item in self._ax_observed:
            err = ApplicationServices.AXObserverAddNotification(
                self._observer, self._ax_element, item, self.obj_pointer
            )
            if err != ApplicationServices.kAXErrorSuccess:
                raise AXAPIError(err)

        Quartz.CFRunLoopAddSource(
            Quartz.CFRunLoopGetCurrent(),
            ApplicationServices.AXObserverGetRunLoopSource(self._observer),
            Quartz.kCFRunLoopCommonModes,
        )

    def __del__(self):
        self.logger.debug("Destructor")
        for item in self._ax_observed:
            ApplicationServices.AXObserverRemoveNotification(
                self._observer, self._ax_element, item
            )

        objc.context.unregister(self)

    def get_position(self) -> QRect:
        # The docs say this API is expensive...
        infos = Quartz.CGWindowListCreateDescriptionFromArray([self.wid])
        info = infos[0]  # FIXME: what if window closed
        return cgrectref_to_qrect(info[Quartz.kCGWindowBounds])

    def get_scaling(self) -> float:
        screen = QGuiApplication.screenAt(self.get_position().topLeft())
        return screen.devicePixelRatio()

    def is_active(self) -> bool:
        return self._is_active

    def update_is_active(self):
        self._is_active = (
            Quartz.NSWorkspace.sharedWorkspace()
            .frontmostApplication()
            .processIdentifier()
            == self.pid
        )

    def grab_game(self) -> Image:
        # FIXME: Crop title bar
        if (time.monotonic() - self.__game_last_grab) * 1000 < self.refresh_rate:
            return self.__game_last_image

        imgref = Quartz.CGWindowListCreateImageFromArray(
            Quartz.CGRectNull,
            [self.wid],
            Quartz.kCGWindowImageBoundsIgnoreFraming,
        )
        out = cgimageref_to_image(imgref)
        scale = self.get_scaling()
        if scale > 1:
            out = out.resize(
                (int(out.width / scale), int(out.height / scale)), Image.NEAREST
            )

        self.__game_last_grab = time.monotonic()
        self.__game_last_image = out
        return out

    def grab_desktop(self, x: int, y: int, w: int, h: int) -> Image:
        imgref = Quartz.CGWindowListCreateImage(
            Quartz.CGRect(Quartz.CGPoint(x, y), Quartz.CGSize(w, h)),
            Quartz.kCGWindowListOptionAll,
            Quartz.kCGNullWindowID,
            Quartz.kCGWindowImageDefault,
        )
        out = cgimageref_to_image(imgref)
        return out.resize((w, h), Image.NEAREST)

    def set_taskbar_progress(self, type, progress):
        pass


class AXAPIError(Exception):
    mapping = {
        ApplicationServices.kAXErrorInvalidUIElementObserver: "The observer is not a valid AXObserverRef type",
        ApplicationServices.kAXErrorIllegalArgument: "One or more of the arguments is an illegal value or the length of the notification name is greater than 1024",
        ApplicationServices.kAXErrorNotificationUnsupported: "The observer is not a valid AXObserverRef type",
        ApplicationServices.kAXErrorNotificationAlreadyRegistered: "The notification has already been registered",
        ApplicationServices.kAXErrorCannotComplete: "The function cannot complete because messaging has failed in some way.",
        ApplicationServices.kAXErrorFailure: "There is some sort of system memory failure.",
    }

    def __init__(self, code):
        if code == 0:
            raise ValueError("Success")

        super().__init__(self.mapping.get(code, f"API Error: {code}"))