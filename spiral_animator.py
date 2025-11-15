# -----------------------------------------------------------------------------
# 2D Animation and Drawing Application - Dynamic Canvas Resizing
#
# Version: 7.2.3 - Fixed Audio Stop/Continuous Live Recording
#
# Installation:
# pip install PyQt6 imageio numpy scipy
# pip install pygame # <-- REQUIREMENT FOR AUDIO PLAYBACK
# pip install imageio-ffmpeg
# -----------------------------------------------------------------------------

import sys
import os
import random
import imageio
import numpy as np
from collections import deque
from scipy.ndimage import label # For fast flood fill

import pygame

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QGraphicsView, QGraphicsScene, QToolBar, QStatusBar,
    QColorDialog, QSlider, QLabel, QDockWidget, QFrame, QScrollArea,
    QFileDialog, QSpinBox, QMessageBox
)
from PyQt6.QtGui import (
    QPixmap, QPainter, QPen, QColor, QAction, QIcon, QImage, QBrush,
    QUndoCommand, QUndoStack
)
from PyQt6.QtCore import Qt, QPoint, QPointF, QSize, QTimer, QRect, QPointF, QRectF

import subprocess
import imageio_ffmpeg

AUDIO_AVAILABLE = True  # Assuming pygame is installed

# --- Helper functions for NumPy conversion ---
def qimage_to_numpy(image):
    """Converts a QImage to a numpy array (creates a copy to avoid corruption)."""
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width = image.width()
    height = image.height()
    
    # Create a proper copy of the image data
    ptr = image.constBits()
    ptr.setsize(height * width * 4)
    
    # Create a copy of the data instead of a view
    arr = np.array(ptr, dtype=np.uint8).reshape((height, width, 4))
    # Return a copy to ensure data persistence
    return arr.copy()

def numpy_to_qimage(arr):
    """Converts a numpy array (h, w, 4) to a QImage."""
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8)
    
    height, width, channels = arr.shape
    if channels != 4:
        raise ValueError("Numpy array must be (h, w, 4) with RGBA channels")
        
    # Create a QImage from the data.
    arr_contiguous = np.ascontiguousarray(arr)
    image = QImage(arr_contiguous.data, width, height, arr_contiguous.strides[0], QImage.Format.Format_RGBA8888)
    
    # We must return a copy because the QImage will be invalidated
    # when arr_contiguous goes out of scope.
    return image.copy()

# -----------------------------------------------------------------------------
# Undo Command (RESTORED)
# -----------------------------------------------------------------------------
class DrawCommand(QUndoCommand):
    """
    An undo command to store a drawing operation (a stroke, a fill, etc.)
    """
    def __init__(self, main_window, frame_index, pixmap_before, pixmap_after, parent=None):
        super().__init__(parent)
        
        self.main_window = main_window
        self.scene = main_window.scene
        self.frame_index = frame_index
        self.pixmap_before = pixmap_before
        self.pixmap_after = pixmap_after
        self.setText("Draw Operation")

    def undo(self):
        """Reverts the frame to its 'before' state."""
        self.main_window.frames[self.frame_index] = self.pixmap_before
        # If we are on the frame that's being undone, refresh the scene
        if self.frame_index == self.main_window.current_frame_index:
            self.main_window.refresh_scene_display()
        self.main_window.update_timeline_thumbnail(self.frame_index)

    def redo(self):
        """Re-applies the frame's 'after' state."""
        self.main_window.frames[self.frame_index] = self.pixmap_after
        # If we are on the frame that's being redone, refresh the scene
        if self.frame_index == self.main_window.current_frame_index:
            self.main_window.refresh_scene_display()
        self.main_window.update_timeline_thumbnail(self.frame_index)

# -----------------------------------------------------------------------------
# Zoomable Graphics View - Handles panning, zooming, and resizing
# -----------------------------------------------------------------------------
class ZoomableGraphicsView(QGraphicsView):
    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.main_window = parent
        self.resizing = False
        self.resize_edge = None # 'left', 'right', 'top', 'bottom'
        self.last_pos = QPoint()
        
        # Set up anchors for smooth zooming
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        
        # Enable anti-aliasing for smoother rendering
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Set background to a darker grey to see the "paper"
        self.setStyleSheet("background-color: #AAAAAA; border: 2px solid #AAAAAA;")

    def wheelEvent(self, event):
        """Handles zooming with Ctrl + Mouse Wheel."""
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            angle = event.angleDelta().y()
            zoom_factor = 1.15 if angle > 0 else 1 / 1.15
            self.scale(zoom_factor, zoom_factor)
        else:
            # Propagate event for other uses (e.g., vertical scroll)
            super().wheelEvent(event)

    def set_pan_mode(self, pan_on):
        """Toggles the panning drag mode."""
        if pan_on:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.unsetCursor()

            
    # --- Canvas Edge Detection for Resizing ---
    def mouseMoveEvent(self, event):
        scene_pos = self.mapToScene(event.pos())
        scene_rect = self.scene().sceneRect()
        margin = 10 # Area around the edge that acts as a handle
        
        if self.resizing:
            # Update last_pos for final calculation (use scene pos for consistency)
            self.last_pos = self.mapToScene(event.pos()).toPoint()
            return
        
        # Cursor change logic only if not drawing or panning
        if not (self.main_window.is_panning or self.scene().drawing):
            cursor = Qt.CursorShape.ArrowCursor
            edge = None
            
            # Check for edges (based on scene coordinates)
            is_near_left = abs(scene_pos.x() - scene_rect.left()) < margin
            is_near_right = abs(scene_pos.x() - scene_rect.right()) < margin
            is_near_top = abs(scene_pos.y() - scene_rect.top()) < margin
            is_near_bottom = abs(scene_pos.y() - scene_rect.bottom()) < margin
            
            if is_near_left and is_near_top:
                cursor, edge = Qt.CursorShape.SizeFDiagCursor, 'topleft'
            elif is_near_right and is_near_top:
                cursor, edge = Qt.CursorShape.SizeBDiagCursor, 'topright'
            elif is_near_left and is_near_bottom:
                cursor, edge = Qt.CursorShape.SizeBDiagCursor, 'bottomleft'
            elif is_near_right and is_near_bottom:
                cursor, edge = Qt.CursorShape.SizeFDiagCursor, 'bottomright'
            elif is_near_left:
                cursor, edge = Qt.CursorShape.SizeHorCursor, 'left'
            elif is_near_right:
                cursor, edge = Qt.CursorShape.SizeHorCursor, 'right'
            elif is_near_top:
                cursor, edge = Qt.CursorShape.SizeVerCursor, 'top'
            elif is_near_bottom:
                cursor, edge = Qt.CursorShape.SizeVerCursor, 'bottom'
            
            self.resize_edge = edge
            self.setCursor(cursor)
        
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if self.resize_edge and event.button() == Qt.MouseButton.LeftButton:
            # We need to store the SCENE position to calculate the change in scene size
            self.resizing = True
            self.last_pos = self.mapToScene(event.pos()).toPoint()
            # Suppress normal scene mouse press if resizing
            return

        super().mousePressEvent(event)
    
    def mouseReleaseEvent(self, event):
        if self.resizing and event.button() == Qt.MouseButton.LeftButton:
            # Use the position *in the scene* for the final calculation
            current_scene_pos = self.mapToScene(event.pos()).toPoint()
            
            # Finalize resize: tell the main window to update all frames/size
            self.main_window.finalize_canvas_resize(self.last_pos, current_scene_pos, self.resize_edge)
            self.resizing = False
            self.resize_edge = None
            self.setCursor(Qt.CursorShape.ArrowCursor) # Reset cursor
            return
            
        super().mouseReleaseEvent(event)
        
    def resize_canvas(self, new_pos):
        """
        Handles visual resize feedback (real-time feedback). 
        The actual permanent resize happens in mouseReleaseEvent.
        """
        # Since we are using the simple deferred method, this is kept minimal.
        pass

# -----------------------------------------------------------------------------
# Drawing Scene - Handles all drawing logic on the canvas
# -----------------------------------------------------------------------------
class DrawingScene(QGraphicsScene):
    def __init__(self, settings, main_window, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.main_window = main_window # For accessing undo_stack
        
        self.current_pixmap_item = None
        self.onion_skin_item = None # For showing the previous frame
        self.paper_item = None # For the white background
        
        self.last_point = QPoint()
        self.drawing = False
        self.pixmap_before_draw = None # For the Undo command
        
        self.setup_paper()

    def setup_paper(self):
        """Creates the white paper background item."""
        if self.paper_item and self.paper_item.scene() == self:
            self.removeItem(self.paper_item)
            
        paper_size = self.main_window.canvas_size
        self.paper_item = self.addRect(
            0, 0, paper_size.width(), paper_size.height(),
            QPen(Qt.PenStyle.NoPen), QBrush(Qt.GlobalColor.white)
        )
        self.paper_item.setZValue(-2) # Sit behind onion skin

    def set_frame(self, pixmap, onion_pixmap=None):
        """
        Sets the current frame (pixmap) to be displayed and drawn on.
        Optionally displays an onion skin pixmap behind it.
        """
        # Clear old items
        if self.current_pixmap_item and self.current_pixmap_item.scene() == self:
            self.removeItem(self.current_pixmap_item)
        if self.onion_skin_item and self.onion_skin_item.scene() == self:
            self.removeItem(self.onion_skin_item)
            
        # Re-assert paper item in case it got cleared (e.g., on new animation)
        if not self.paper_item or not self.paper_item.scene():
            self.setup_paper()
            
        # Add onion skin (previous frame) if provided
        if onion_pixmap:
            self.onion_skin_item = self.addPixmap(onion_pixmap)
            self.onion_skin_item.setZValue(-1)
            self.onion_skin_item.setOpacity(0.3) # Faintly visible
            
        # Add current frame (drawing layer)
        self.current_pixmap_item = self.addPixmap(pixmap)
        self.current_pixmap_item.setZValue(0)
        
        # Reset scene rect to encompass all items
        self.setSceneRect(0, 0, self.main_window.canvas_size.width(), self.main_window.canvas_size.height())
        
    def get_current_pixmap(self):
        """Returns the pixmap of the currently displayed frame."""
        if self.current_pixmap_item:
            return self.current_pixmap_item.pixmap()
        return None

    def mousePressEvent(self, event):
        # Prevent drawing if panning is active
        if self.main_window.is_panning:
            super().mousePressEvent(event)
            return
        
        # Prevent drawing if resizing is active (handled by ZoomableGraphicsView)
        if isinstance(self.main_window.canvas_view, ZoomableGraphicsView) and self.main_window.canvas_view.resizing:
            return

        if event.button() == Qt.MouseButton.LeftButton:
            if self.settings['tool'] == 'fill':
                # Flood fill is a single operation, create undo command now
                self.pixmap_before_draw = self.get_current_pixmap().copy()
                self.flood_fill(event.scenePos().toPoint())
            else:
                # Start of a drawing stroke
                self.drawing = True
                self.last_point = event.scenePos()
                
                # Only save for undo if NOT playing
                if not self.main_window.is_playing:
                    # Store the 'before' state for the undo command
                    self.pixmap_before_draw = self.get_current_pixmap().copy()

    def mouseMoveEvent(self, event):
        # Prevent drawing if panning is active
        if self.main_window.is_panning:
            super().mouseMoveEvent(event)
            return

        if self.drawing and (event.buttons() & Qt.MouseButton.LeftButton):
            if self.settings['tool'] != 'fill':
                self.draw_line(self.last_point, event.scenePos())
            self.last_point = event.scenePos()

    def mouseReleaseEvent(self, event):
        # Prevent drawing if panning is active
        if self.main_window.is_panning:
            super().mouseReleaseEvent(event)
            return

        if event.button() == Qt.MouseButton.LeftButton and self.drawing:
            # End of a drawing stroke
            self.drawing = False
            
            # Only create an undo command if we are NOT playing
            if not self.main_window.is_playing and self.settings['tool'] != 'fill':
                # Create the undo command with the 'before' and 'after' states
                pixmap_after = self.get_current_pixmap().copy()
                command = DrawCommand(
                    self.main_window, 
                    self.main_window.current_frame_index,
                    self.pixmap_before_draw, 
                    pixmap_after
                )
                self.main_window.undo_stack.push(command)
                self.main_window.update_timeline_thumbnail(self.main_window.current_frame_index)

    def _get_wavery_point(self, point):
        """Applies a 'drunken' effect to a point."""
        drunkenness = self.settings.get('drunkenness', 0)
        if drunkenness == 0:
            return point
        offset_x = (random.random() - 0.5) * drunkenness
        offset_y = (random.random() - 0.5) * drunkenness
        return QPointF(point.x() + offset_x, point.y() + offset_y)

    def draw_point(self, point):
        """Draws a single point or a spray cluster."""
        pixmap = self.current_pixmap_item.pixmap()
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        tool = self.settings['tool']
        
        if tool == 'eraser':
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
            pen = QPen(QColor(0, 0, 0, 255))
            pen.setWidth(self.settings.get('size', 10))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawPoint(self._get_wavery_point(point).toPoint())
        elif tool in ['pencil', 'brush']:
            pen = self._get_pen()
            painter.setPen(pen)
            painter.drawPoint(self._get_wavery_point(point).toPoint())
        elif tool == 'spray':
            self._spray_paint(painter, point)

        painter.end()
        self.current_pixmap_item.setPixmap(pixmap)

    def draw_line(self, start_point, end_point):
        """Draws a line based on the current tool."""
        pixmap = self.current_pixmap_item.pixmap()
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        tool = self.settings['tool']
        
        if tool == 'eraser':
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
            pen = QPen(QColor(0, 0, 0, 255))
            pen.setWidth(self.settings.get('size', 10))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(self._get_wavery_point(start_point), self._get_wavery_point(end_point))
        elif tool in ['pencil', 'brush']:
            pen = self._get_pen()
            painter.setPen(pen)
            painter.drawLine(self._get_wavery_point(start_point), self._get_wavery_point(end_point))
        elif tool == 'spray':
            self._spray_paint(painter, end_point)

        painter.end()
        self.current_pixmap_item.setPixmap(pixmap)
    
    def _spray_paint(self, painter, center_point):
        """Simulates a spray paint effect."""
        color = QColor(self.settings['color'])
        opacity = self.settings.get('opacity', 100)
        color.setAlphaF(opacity / 100.0)
        
        pen = QPen(color)
        pen.setWidth(max(1, self.settings.get('size', 10) // 4))
        painter.setPen(pen)
        
        density = self.settings.get('size', 10) * 2
        radius = self.settings.get('size', 10) * 1.5
        
        for _ in range(density):
            offset_x = (random.random() - 0.5) * radius * 2
            offset_y = (random.random() - 0.5) * radius * 2
            point = QPointF(center_point.x() + offset_x, center_point.y() + offset_y)
            painter.drawPoint(self._get_wavery_point(point).toPoint())

    def _get_pen(self):
        """Creates a QPen object based on current settings."""
        pen = QPen()
        color = QColor(self.settings['color'])
        opacity = self.settings.get('opacity', 100)
        color.setAlphaF(opacity / 100.0)
        pen.setColor(color)

        pen.setWidth(self.settings.get('size', 10))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

        if self.settings['tool'] == 'pencil':
            pen.setCapStyle(Qt.PenCapStyle.SquareCap)
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            
        return pen

    def flood_fill(self, start_point):
        """Optimized flood fill using NumPy and SciPy."""
        pixmap = self.get_current_pixmap()
        if not pixmap:
            return

        image = pixmap.toImage()
        w, h = image.width(), image.height()
        
        # Floor the start point to get integer coordinates
        sp = start_point
        x, y = sp.x(), sp.y()

        # Check if starting point is within bounds
        if not (0 <= x < w and 0 <= y < h):
            return
            
        # Get target color
        target_color = image.pixelColor(x, y)
        target_rgba_int = target_color.rgba()

        # Get fill color
        fill_color_q = QColor(self.settings['color'])
        opacity = self.settings.get('opacity', 100)
        fill_color_q.setAlphaF(opacity / 100.0)
        
        # Convert QColor's RGBA (which is a single int)
        fill_rgba_int = fill_color_q.rgba()
        
        # Check if target is already the fill color
        if target_rgba_int == fill_rgba_int:
            return

        # Convert image to NumPy array
        arr = qimage_to_numpy(image)

        # Convert target_rgba (int) to a (R, G, B, A) tuple
        target_rgba_tuple = (target_color.red(), target_color.green(), target_color.blue(), target_color.alpha())

        # Create a 2D boolean mask where the color matches the target
        mask = np.all(arr == target_rgba_tuple, axis=2)

        # Use scipy.ndimage.label to find connected components
        labels, num_features = label(mask, structure=np.array([[0,1,0],[1,1,1],[0,1,0]]))
        
        # Get the label ID of the component we clicked on
        clicked_label = labels[y, x]
        
        # If we clicked on a non-target area (label 0), do nothing
        if clicked_label == 0:
            return

        # Create a mask for all pixels with the same label as the one we clicked
        fill_mask = (labels == clicked_label)

        # Get the fill color as a tuple for NumPy
        fill_rgba_tuple = (fill_color_q.red(), fill_color_q.green(), fill_color_q.blue(), fill_color_q.alpha())

        # Apply the fill color to the masked area
        arr[fill_mask] = fill_rgba_tuple
        
        # Convert back to QImage/QPixmap
        filled_image = numpy_to_qimage(arr)
        pixmap_after = QPixmap.fromImage(filled_image)

        # Create the undo command
        command = DrawCommand(
            self.main_window, 
            self.main_window.current_frame_index,
            self.pixmap_before_draw, 
            pixmap_after
        )
        self.main_window.undo_stack.push(command)
        
        # Update the scene
        self.current_pixmap_item.setPixmap(pixmap_after)
        self.main_window.update_timeline_thumbnail(self.main_window.current_frame_index)
        
# -----------------------------------------------------------------------------
# Main Application Window
# -----------------------------------------------------------------------------
class AnimatorApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("2D Animation Studio (v7.2.3)")
        self.setGeometry(100, 100, 1200, 800)

        if AUDIO_AVAILABLE:
            pygame.mixer.init()

        # --- Application State ---
        self.frames = []
        self.current_frame_index = -1
        self.canvas_size = QSize(800, 600)
        
        self.settings = {
            'tool': 'brush',
            'color': QColor('black'),
            'size': 15,
            'opacity': 100,
            'drunkenness': 0
        }

        # --- Animation Playback ---
        self.playback_timer = QTimer(self)
        self.playback_timer.timeout.connect(self.next_frame)
        self.is_playing = False
        
        # --- Onion Skinning ---
        self.onion_skin_enabled = False

        # --- Undo/Redo ---
        self.undo_stack = QUndoStack(self)
        
        # --- View State ---
        self.is_panning = False

        # --- Image Asset State ---
        self.image_folder_path = None
        self.image_file_list = []

        # --- Audio Asset State ---
        self.audio_folder_path = None
        self.audio_file_list = []
        self.current_audio_track = None
        self.current_audio_index = -1
        self.is_audio_paused = False

        # --- Live Recording State ---
        self.is_recording = False
        self.recording_writer = None
        self.recording_path = None
        
        self.init_ui()
        self.add_new_frame()

    def init_ui(self):
        # --- Central Canvas ---
        # Pass 'self' (the main window) to the scene for undo/redo
        self.scene = DrawingScene(self.settings, self, self) 
        # Use our new ZoomableGraphicsView
        self.canvas_view = ZoomableGraphicsView(self.scene, self)
        self.setCentralWidget(self.canvas_view)

        # --- Tool Settings Dock ---
        self.create_settings_dock()

        # --- Assets Dock (Includes Audio) ---
        self.create_assets_dock()

        # --- Frame Timeline Dock ---
        self.create_timeline_dock()
        
        # --- Toolbar ---
        self.create_toolbar()
        
        # --- Menu and Status Bar ---
        self.create_menu_bar()
        self.setStatusBar(QStatusBar(self))
        self.update_status()

    # --- UI Creation Methods ---
    def create_toolbar(self):
        toolbar = QToolBar("Main Toolbar")
        self.addToolBar(toolbar)

        # Tool action group
        self.tool_actions = []
        
        tools = {
            "pencil": ("✏️", "Pencil (P)"), 
            "brush": ("🖌️", "Brush (B)"), 
            "eraser": ("🧹", "Eraser (E)"),
            "spray": ("💨", "Spray (S)"), 
            "fill": ("🪣", "Fill (F)")
        }
        
        for tool_name, (icon_text, tooltip) in tools.items():
            action = QAction(icon_text, self)
            action.setStatusTip(tooltip)
            action.triggered.connect(lambda checked, t=tool_name: self.set_tool(t))
            action.setCheckable(True)
            if tool_name == self.settings['tool']:
                action.setChecked(True)
            toolbar.addAction(action)
            self.tool_actions.append(action)
        
        toolbar.addSeparator()
        
        # --- Animation Controls ---
        self.play_action = QAction("▶️ Play", self)
        self.play_action.triggered.connect(self.toggle_playback)
        toolbar.addAction(self.play_action)

        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(1, 60)
        self.speed_slider.setValue(12)
        self.speed_slider.setMaximumWidth(100)
        self.speed_slider.setToolTip("Playback Speed (FPS)")
        self.speed_slider.valueChanged.connect(self.update_playback_speed)
        
        fps_label = QLabel(" FPS: ")
        toolbar.addWidget(fps_label)
        toolbar.addWidget(self.speed_slider)
        
        self.fps_display = QLabel("12")
        self.speed_slider.valueChanged.connect(lambda v: self.fps_display.setText(str(v)))
        toolbar.addWidget(self.fps_display)
        
        toolbar.addSeparator()
        
        # --- Onion Skin Button ---
        self.onion_skin_action = QAction("🧅 Onion Skin", self)
        self.onion_skin_action.setCheckable(True)
        self.onion_skin_action.toggled.connect(self.toggle_onion_skin)
        toolbar.addAction(self.onion_skin_action)

    def create_settings_dock(self):
        dock = QDockWidget("Tool Settings", self)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        
        settings_widget = QWidget()
        layout = QVBoxLayout(settings_widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Color Button
        self.color_button = QPushButton("Color")
        self.color_button.setFixedHeight(40)
        self.update_color_button()
        self.color_button.clicked.connect(self.select_color)
        layout.addWidget(self.color_button)

        # Size Slider
        size_label = QLabel("Size")
        layout.addWidget(size_label)
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(1, 200)
        self.size_slider.setValue(self.settings['size'])
        self.size_slider.valueChanged.connect(lambda v: self.update_setting('size', v))
        layout.addWidget(self.size_slider)
        
        self.size_display = QLabel(f"Size: {self.settings['size']}")
        self.size_slider.valueChanged.connect(lambda v: self.size_display.setText(f"Size: {v}"))
        layout.addWidget(self.size_display)

        # Opacity Slider
        opacity_label = QLabel("Opacity")
        layout.addWidget(opacity_label)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(self.settings['opacity'])
        self.opacity_slider.valueChanged.connect(lambda v: self.update_setting('opacity', v))
        layout.addWidget(self.opacity_slider)
        
        self.opacity_display = QLabel(f"Opacity: {self.settings['opacity']}%")
        self.opacity_slider.valueChanged.connect(lambda v: self.opacity_display.setText(f"Opacity: {v}%"))
        layout.addWidget(self.opacity_display)

        # Drunkenness Slider
        drunken_label = QLabel("Waver / Drunkenness")
        layout.addWidget(drunken_label)
        self.drunken_slider = QSlider(Qt.Orientation.Horizontal)
        self.drunken_slider.setRange(0, 50)
        self.drunken_slider.setValue(self.settings['drunkenness'])
        self.drunken_slider.valueChanged.connect(lambda v: self.update_setting('drunkenness', v))
        layout.addWidget(self.drunken_slider)
        
        self.drunken_display = QLabel(f"Waver: {self.settings['drunkenness']}")
        self.drunken_slider.valueChanged.connect(lambda v: self.drunken_display.setText(f"Waver: {v}"))
        layout.addWidget(self.drunken_display)
        
        dock.setWidget(settings_widget)
    
    def update_color_button(self):
        """Update color button appearance."""
        color = self.settings['color']
        # Calculate contrasting text color
        brightness = (color.red() * 299 + color.green() * 587 + color.blue() * 114) / 1000
        text_color = "white" if brightness < 128 else "black"
        self.color_button.setStyleSheet(
            f"background-color: {color.name()}; "
            f"color: {text_color}; "
            f"font-weight: bold; "
            f"border: 2px solid #888;"
        )
    
    def create_timeline_dock(self):
        self.timeline_dock = QDockWidget("Frames Timeline", self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.timeline_dock)

        timeline_container = QWidget()
        container_layout = QVBoxLayout(timeline_container)
        
        # Control buttons
        controls_widget = QWidget()
        controls_layout = QHBoxLayout(controls_widget)
        controls_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        
        add_frame_btn = QPushButton("➕ Add Frame")
        add_frame_btn.setFixedHeight(30)
        add_frame_btn.clicked.connect(self.add_new_frame)
        controls_layout.addWidget(add_frame_btn)

        del_frame_btn = QPushButton("➖ Delete Frame")
        del_frame_btn.setFixedHeight(30)
        del_frame_btn.clicked.connect(self.delete_current_frame)
        controls_layout.addWidget(del_frame_btn)
        
        duplicate_btn = QPushButton("📋 Duplicate")
        duplicate_btn.setFixedHeight(30)
        duplicate_btn.clicked.connect(self.duplicate_current_frame)
        controls_layout.addWidget(duplicate_btn)
        
        controls_layout.addStretch()
        container_layout.addWidget(controls_widget)
        
        # Scrollable timeline
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setMaximumHeight(120)
        
        self.timeline_widget = QWidget()
        self.timeline_layout = QHBoxLayout(self.timeline_widget)
        self.timeline_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        
        scroll_area.setWidget(self.timeline_widget)
        container_layout.addWidget(scroll_area)
        
        self.timeline_dock.setWidget(timeline_container)

    def create_menu_bar(self):
        menu = self.menuBar()
        
        # File Menu
        file_menu = menu.addMenu("&File")
        
        new_action = QAction("New Animation...", self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(self.new_animation)
        file_menu.addAction(new_action)
        
        import_action = QAction("Import Image...", self)
        import_action.setShortcut("Ctrl+I")
        import_action.triggered.connect(self.import_image)
        file_menu.addAction(import_action)
        
        set_folder_action = QAction("Set Random Image Folder...", self)
        set_folder_action.triggered.connect(self.set_image_folder)
        file_menu.addAction(set_folder_action)
        
        set_audio_folder_action = QAction("Set Audio Folder...", self)
        set_audio_folder_action.triggered.connect(self.set_audio_folder)
        file_menu.addAction(set_audio_folder_action)
        
        file_menu.addSeparator()
        
        self.export_menu = file_menu.addMenu("Export As")
        
        # --- LIVE RECORDING (Records duration until STOP) ---
        self.live_record_gif_action = QAction("🔴 Record LIVE GIF (Loop)", self)
        self.live_record_gif_action.triggered.connect(lambda: self.start_live_recording('gif'))
        self.export_menu.addAction(self.live_record_gif_action)
        
        self.live_record_mp4_action = QAction("🔴 Record LIVE MP4 (Loop)", self)
        self.live_record_mp4_action.triggered.connect(lambda: self.start_live_recording('mp4'))
        self.export_menu.addAction(self.live_record_mp4_action)

        self.stop_recording_action = QAction("⏹️ Stop Recording", self)
        self.stop_recording_action.triggered.connect(self.stop_recording)
        
        self.export_menu.addSeparator()
        
        # --- OFFLINE EXPORT (Records entire timeline once) ---
        self.export_gif_action = QAction("Export Timeline GIF (Once)", self)
        self.export_gif_action.triggered.connect(lambda: self.export_video_file('gif'))
        self.export_menu.addAction(self.export_gif_action)
        
        self.export_mp4_action = QAction("Export Timeline MP4 (Once)", self)
        self.export_mp4_action.triggered.connect(lambda: self.export_video_file('mp4'))
        self.export_menu.addAction(self.export_mp4_action)

        self.export_seq_action = QAction("Export Image Sequence (PNG)...", self)
        self.export_seq_action.triggered.connect(self.export_image_sequence)
        self.export_menu.addAction(self.export_seq_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("Exit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # Edit Menu
        edit_menu = menu.addMenu("&Edit")
        
        # Add Undo/Redo actions
        undo_action = self.undo_stack.createUndoAction(self, "&Undo")
        undo_action.setShortcut("Ctrl+Z")
        edit_menu.addAction(undo_action)
        
        redo_action = self.undo_stack.createRedoAction(self, "&Redo")
        redo_action.setShortcut("Ctrl+Y")
        edit_menu.addAction(redo_action)
        
        edit_menu.addSeparator()
        
        clear_frame_action = QAction("Clear Frame", self)
        clear_frame_action.setShortcut("Delete")
        clear_frame_action.triggered.connect(self.clear_current_frame)
        edit_menu.addAction(clear_frame_action)
        
        duplicate_action = QAction("Duplicate Frame", self)
        duplicate_action.setShortcut("Ctrl+D")
        duplicate_action.triggered.connect(self.duplicate_current_frame)
        edit_menu.addAction(duplicate_action)
        
        # View Menu
        view_menu = menu.addMenu("&View")
        
        reset_view_action = QAction("Reset View", self)
        reset_view_action.setShortcut("Ctrl+0")
        reset_view_action.triggered.connect(self.reset_view)
        view_menu.addAction(reset_view_action)

    def create_assets_dock(self):
        """Creates the dock for random image assets and audio controls."""
        dock = QDockWidget("Random Assets & Audio", self)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        
        assets_widget = QWidget()
        layout = QVBoxLayout(assets_widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        # --- Image Controls ---
        image_label = QLabel("Image Thrower")
        image_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(image_label)
        
        self.set_folder_btn = QPushButton("Set Image Folder...")
        self.set_folder_btn.clicked.connect(self.set_image_folder)
        layout.addWidget(self.set_folder_btn)
        
        self.folder_label = QLabel("No image folder set.")
        self.folder_label.setWordWrap(True)
        layout.addWidget(self.folder_label)
        
        self.throw_image_btn = QPushButton("Throw Random Image")
        self.throw_image_btn.clicked.connect(self.throw_random_image)
        self.throw_image_btn.setEnabled(False) 
        layout.addWidget(self.throw_image_btn)
        
        layout.addSpacing(10)
        
        # Image Opacity Slider
        self.image_opacity_label = QLabel("Image Opacity: 100%")
        layout.addWidget(self.image_opacity_label)
        
        self.image_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.image_opacity_slider.setRange(1, 100)
        self.image_opacity_slider.setValue(100)
        self.image_opacity_slider.valueChanged.connect(
            lambda v: self.image_opacity_label.setText(f"Image Opacity: {v}%")
        )
        layout.addWidget(self.image_opacity_slider)
        
        layout.addSpacing(30)
        
        # --- Audio Controls ---
        audio_label = QLabel("Audio Controls")
        audio_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(audio_label)
        
        set_audio_btn = QPushButton("Set Audio Folder...")
        set_audio_btn.clicked.connect(self.set_audio_folder)
        layout.addWidget(set_audio_btn)
        
        self.audio_folder_label = QLabel("No audio folder set.")
        self.audio_folder_label.setWordWrap(True)
        layout.addWidget(self.audio_folder_label)
        
        self.audio_controls_widget = QWidget()
        audio_layout = QHBoxLayout(self.audio_controls_widget)
        audio_layout.setContentsMargins(0, 0, 0, 0)
        
        self.next_audio_btn = QPushButton("Next Random Track")
        self.next_audio_btn.clicked.connect(self.next_audio_track)
        self.next_audio_btn.setEnabled(False)
        audio_layout.addWidget(self.next_audio_btn)
        
        self.pause_audio_btn = QPushButton("Pause Audio")
        self.pause_audio_btn.clicked.connect(self.toggle_audio_pause)
        self.pause_audio_btn.setEnabled(False)
        audio_layout.addWidget(self.pause_audio_btn)
        
        audio_layout.addStretch()
        layout.addWidget(self.audio_controls_widget)
        
        self.current_audio_display = QLabel("No track loaded.")
        self.current_audio_display.setWordWrap(True)
        layout.addWidget(self.current_audio_display)
        
        layout.addStretch()
        dock.setWidget(assets_widget)

    # --- Core Logic ---
    def new_animation(self):
        if self.is_recording:
            QMessageBox.warning(self, "Recording Active", "Please stop recording before creating a new animation.")
            return

        reply = QMessageBox.question(self, "New Animation", 
                                   "This will clear all frames. Continue?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.frames.clear()
            self.current_frame_index = -1
            self.canvas_size = QSize(800, 600) # Reset to default size
            self.undo_stack.clear()
            self.clear_timeline()
            self.add_new_frame()
            self.scene.setup_paper()
            self.update_status()
            self.reset_view()
            self.stop_audio() # Stop audio on new animation

    def clear_timeline(self):
        """Clear all frame widgets from timeline."""
        while self.timeline_layout.count() > 0:
            item = self.timeline_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def add_new_frame(self):
        """Add a new blank frame after the current frame."""
        # Save current frame first
        if self.current_frame_index >= 0:
            current_pixmap = self.scene.get_current_pixmap()
            if current_pixmap:
                self.frames[self.current_frame_index] = current_pixmap.copy()
        
        pixmap = QPixmap(self.canvas_size)
        pixmap.fill(Qt.GlobalColor.transparent) # Transparent background
        self.frames.insert(self.current_frame_index + 1, pixmap)
        self.set_current_frame(self.current_frame_index + 1)
        self.update_timeline()

    def duplicate_current_frame(self):
        """Duplicate the current frame."""
        if self.current_frame_index >= 0:
            # Save current frame first
            current_pixmap = self.scene.get_current_pixmap()
            if current_pixmap:
                self.frames[self.current_frame_index] = current_pixmap.copy()
            
            # Duplicate it
            duplicated = self.frames[self.current_frame_index].copy()
            self.frames.insert(self.current_frame_index + 1, duplicated)
            self.set_current_frame(self.current_frame_index + 1)
            self.update_timeline()

    def delete_current_frame(self):
        if self.is_recording: return

        if len(self.frames) > 1 and self.current_frame_index >= 0:
            self.frames.pop(self.current_frame_index)
            self.undo_stack.clear() # History is gone for this frame
            new_index = min(self.current_frame_index, len(self.frames) - 1)
            self.current_frame_index = new_index
            self.refresh_scene_display()
            self.update_status()
            self.update_timeline()
        elif len(self.frames) == 1:
            QMessageBox.warning(self, "Cannot Delete", "Cannot delete the last frame.")

    def set_current_frame(self, index):
        """
        Change the currently active frame. This clears the undo stack.
        """
        if 0 <= index < len(self.frames):
            if self.current_frame_index == index:
                return # Already on this frame
                
            # Save the current drawing back to its pixmap before switching
            if self.current_frame_index >= 0 and self.current_frame_index < len(self.frames):
                current_pixmap = self.scene.get_current_pixmap()
                if current_pixmap:
                    self.frames[self.current_frame_index] = current_pixmap.copy()

            self.current_frame_index = index
            self.refresh_scene_display()
            self.update_status()
            self.update_timeline_selection()
            
            # Clear undo stack, as it's for a different frame
            self.undo_stack.clear()
    
    def refresh_scene_display(self):
        """
        Reloads the current frame into the scene, redrawing onion skin.
        Used by undo/redo and frame changing.
        """
        if not (0 <= self.current_frame_index < len(self.frames)):
            return
            
        current_pixmap = self.frames[self.current_frame_index]
        onion_pixmap = None
        
        if self.onion_skin_enabled and self.current_frame_index > 0:
            onion_pixmap = self.frames[self.current_frame_index - 1]
            
        self.scene.set_frame(current_pixmap, onion_pixmap)
    
    def clear_current_frame(self):
        """Clears the frame and creates an undo action for it."""
        if self.current_frame_index >= 0:
            pixmap_before = self.frames[self.current_frame_index]
            
            pixmap_after = QPixmap(self.canvas_size)
            pixmap_after.fill(Qt.GlobalColor.transparent) # Transparent background
            
            # Create undo command
            command = DrawCommand(
                self, 
                self.current_frame_index,
                pixmap_before, 
                pixmap_after
            )
            self.undo_stack.push(command)
            
            # Apply the change
            self.frames[self.current_frame_index] = pixmap_after
            self.refresh_scene_display()
            self.update_timeline_thumbnail(self.current_frame_index)

    # --- Timeline Management ---
    def update_timeline(self):
        """Rebuild all timeline thumbnails."""
        self.clear_timeline()
        
        for i, pixmap in enumerate(self.frames):
            frame_btn = QPushButton()
            frame_btn.setCheckable(True)
            frame_btn.setFixedSize(100, 80)
            
            # Create a white-backed thumbnail for visibility
            thumbnail_pixmap = QPixmap(90, 70)
            thumbnail_pixmap.fill(Qt.GlobalColor.white)
            painter = QPainter(thumbnail_pixmap)
            scaled_frame = pixmap.scaled(90, 70, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(0, 0, scaled_frame)
            painter.end()

            frame_btn.setIcon(QIcon(thumbnail_pixmap))
            frame_btn.setIconSize(QSize(90, 70))
            
            frame_btn.setText(f"{i+1}")
            frame_btn.setStyleSheet("""
                QPushButton { border: 2px solid #ccc; border-radius: 4px; text-align: bottom; padding-bottom: 2px; }
                QPushButton:checked { border: 3px solid #4CAF50; background-color: #e8f5e9; }
            """)
            
            frame_btn.clicked.connect(lambda checked, idx=i: self.set_current_frame(idx))
            self.timeline_layout.addWidget(frame_btn)

        self.update_timeline_selection()

    def update_timeline_thumbnail(self, index):
        """Updates just one thumbnail in the timeline."""
        if 0 <= index < self.timeline_layout.count():
            widget = self.timeline_layout.itemAt(index).widget()
            if isinstance(widget, QPushButton):
                pixmap = self.frames[index]
                
                # Create a white-backed thumbnail for visibility
                thumbnail_pixmap = QPixmap(90, 70)
                thumbnail_pixmap.fill(Qt.GlobalColor.white)
                painter = QPainter(thumbnail_pixmap)
                scaled_frame = pixmap.scaled(90, 70, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                painter.drawPixmap(0, 0, scaled_frame)
                painter.end()
                
                widget.setIcon(QIcon(thumbnail_pixmap))
        
    def update_timeline_selection(self):
        """Update which frame button is selected."""
        for i in range(self.timeline_layout.count()):
            widget = self.timeline_layout.itemAt(i).widget()
            if isinstance(widget, QPushButton):
                widget.setChecked(i == self.current_frame_index)

    # --- Settings and Status ---
    def set_tool(self, tool):
        self.settings['tool'] = tool
        self.update_status()
        
        for action in self.tool_actions:
            action.setChecked(tool in action.statusTip().lower())

    def select_color(self):
        color = QColorDialog.getColor(self.settings['color'], self, "Select Color")
        if color.isValid():
            self.settings['color'] = color
            self.update_color_button()
            self.update_status()

    def update_setting(self, key, value):
        self.settings[key] = value
        self.update_status()

    def update_status(self):
        if self.is_recording:
            self.statusBar().showMessage(f"🔴 RECORDING to {os.path.basename(self.recording_path)}...")
            return

        color_name = self.settings['color'].name()
        tool = self.settings['tool'].capitalize()
        size = self.settings['size']
        opacity = self.settings['opacity']
        frame_info = f"{self.current_frame_index + 1}/{len(self.frames)}"
        
        self.statusBar().showMessage(
            f"Tool: {tool} | Size: {size}px | Opacity: {opacity}% | "
            f"Color: {color_name} | Frame: {frame_info}"
        )

    # --- Animation Playback ---
    def toggle_playback(self):
        if self.is_playing:
            self.playback_timer.stop()
            self.is_playing = False
            self.play_action.setText("▶️ Play")
        else:
            if self.current_frame_index >= 0:
                current_pixmap = self.scene.get_current_pixmap()
                if current_pixmap:
                    self.frames[self.current_frame_index] = current_pixmap.copy()
            
            self.is_playing = True
            self.play_action.setText("⏸️ Pause")
            self.update_playback_speed()

    def update_playback_speed(self):
        if self.is_playing:
            fps = self.speed_slider.value()
            if fps > 0:
                self.playback_timer.start(1000 // fps)

    def next_frame(self):
        if len(self.frames) > 0:
            
            # Save the current drawing back to its pixmap before switching
            # This is what enables "live drawing" during playback.
            if self.current_frame_index >= 0 and self.current_frame_index < len(self.frames):
                current_pixmap = self.scene.get_current_pixmap()
                if current_pixmap:
                    self.frames[self.current_frame_index] = current_pixmap.copy()
            
            # We are just *displaying* frames, not *editing* them.
            # So we use a lightweight frame change that doesn't clear undo.
            next_idx = (self.current_frame_index + 1) % len(self.frames)
            
            self.current_frame_index = next_idx
            self.refresh_scene_display()
            self.update_status()
            self.update_timeline_selection()

            if self.is_recording and self.recording_writer:
                # Get the current frame and draw it on a white background
                frame_pixmap = self.scene.get_current_pixmap()
                if frame_pixmap:
                    bg_pixmap = QPixmap(self.canvas_size)
                    bg_pixmap.fill(Qt.GlobalColor.white)
                    painter = QPainter(bg_pixmap)
                    painter.drawPixmap(0, 0, frame_pixmap)
                    painter.end()
                
                    numpy_image = qimage_to_numpy(bg_pixmap.toImage())
                    self.recording_writer.append_data(numpy_image)

    # --- Onion Skinning ---
    def toggle_onion_skin(self, checked):
        """Turns onion skinning on or off."""
        self.onion_skin_enabled = checked
        self.refresh_scene_display() # Redraw scene to show/hide onion skin

    # --- View Controls ---
    def reset_view(self):
        """Resets the view transform to default."""
        self.canvas_view.resetTransform()
        
    def keyPressEvent(self, event):
        """Handle keyboard shortcuts for tools and panning."""
        if event.isAutoRepeat():
            return
            
        key = event.key()
        
        if key == Qt.Key.Key_Space:
            self.is_panning = True
            self.canvas_view.set_pan_mode(True)
        elif key == Qt.Key.Key_B:
            self.set_tool('brush')
        elif key == Qt.Key.Key_P:
            self.set_tool('pencil')
        elif key == Qt.Key.Key_E:
            self.set_tool('eraser')
        elif key == Qt.Key.Key_S:
            self.set_tool('spray')
        elif key == Qt.Key.Key_F:
            self.set_tool('fill')
        else:
            super().keyPressEvent(event)
            
    def keyReleaseEvent(self, event):
        """Stop panning when spacebar is released."""
        if event.isAutoRepeat():
            return
            
        if event.key() == Qt.Key.Key_Space:
            self.is_panning = False
            self.canvas_view.set_pan_mode(False)
        else:
            super().keyReleaseEvent(event)

    # --- Canvas Resizing Logic ---
    def finalize_canvas_resize(self, last_scene_pos, current_scene_pos, edge):
        """
        Calculates size/position changes and applies them to all frames.
        last_scene_pos and current_scene_pos are QPoints in the scene coordinate system.
        """
        
        # 1. Calculate the delta/shift
        shift_x, shift_y = 0, 0 # How much content needs to shift (for left/top drags)
        new_w, new_h = self.canvas_size.width(), self.canvas_size.height()
        old_w, old_h = new_w, new_h
        
        # Deltas are calculated directly from scene coordinates
        delta_x = current_scene_pos.x() - last_scene_pos.x()
        delta_y = current_scene_pos.y() - last_scene_pos.y()
        
        # Calculate new width/height and the required content shift
        if 'left' in edge:
            new_w -= int(delta_x)
            shift_x = int(delta_x)
        if 'right' in edge:
            new_w += int(delta_x)
        if 'top' in edge:
            new_h -= int(delta_y)
            shift_y = int(delta_y)
        if 'bottom' in edge:
            new_h += int(delta_y)
            
        # Ensure minimum size
        min_size = QSize(100, 100)
        new_w = max(new_w, min_size.width())
        new_h = max(new_h, min_size.height())
        
        # Adjust shift if clamped
        if 'left' in edge:
            shift_x = old_w - new_w
        if 'top' in edge:
            shift_y = old_h - new_h

        # Don't proceed if no effective size change
        if new_w == old_w and new_h == old_h:
            return

        # 2. Resize and Reposition Frames
        new_size = QSize(int(new_w), int(new_h))
        resized_frames = []
        
        for old_pixmap in self.frames:
            new_pixmap = QPixmap(new_size)
            new_pixmap.fill(Qt.GlobalColor.transparent)
            
            painter = QPainter(new_pixmap)
            # Use the calculated shift to position the old content
            painter.drawPixmap(shift_x, shift_y, old_pixmap)
            painter.end()
            
            resized_frames.append(new_pixmap)

        # 3. Update Application State
        self.canvas_size = new_size
        self.frames = resized_frames
        
        # 4. Refresh UI
        self.scene.setup_paper() # Update paper size
        self.refresh_scene_display() # Redraw current frame
        self.update_timeline() # Update thumbnails
        self.update_status()
        self.reset_view() # Zoom out to see the new canvas boundaries

    # --- Image Import ---
    def paste_image_on_current_frame(self, image_path):
        """Helper function to load an image, scale it, and paste it."""
        try:
            imported_pixmap = QPixmap(image_path)
            if imported_pixmap.isNull():
                print(f"Error: Could not load image {image_path}")
                self.statusBar().showMessage(f"Error: Could not load image {image_path}", 5000)
                return

            # Scale image to fit canvas, maintaining aspect ratio
            scaled_pixmap = imported_pixmap.scaled(self.canvas_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            
            # --- Undo/Redo Logic ---
            pixmap_before = self.frames[self.current_frame_index].copy()
            pixmap_after = pixmap_before.copy()
            
            painter = QPainter(pixmap_after)
            
            # --- SET OPACITY FROM SLIDER ---
            opacity = self.image_opacity_slider.value() / 100.0
            painter.setOpacity(opacity)
            
            # Center the image
            x = (self.canvas_size.width() - scaled_pixmap.width()) // 2
            y = (self.canvas_size.height() - scaled_pixmap.height()) // 2
            painter.drawPixmap(x, y, scaled_pixmap)
            painter.end()

            # Only create undo command if we are not playing
            if not self.is_playing:
                command = DrawCommand(
                    self, 
                    self.current_frame_index,
                    pixmap_before, 
                    pixmap_after
                )
                self.undo_stack.push(command)
            
            # Apply the change
            self.frames[self.current_frame_index] = pixmap_after
            self.refresh_scene_display()
            self.update_timeline_thumbnail(self.current_frame_index)

        except Exception as e:
            QMessageBox.critical(self, "Image Error", f"Could not paste image:\n{e}")

    def import_image(self):
        """Opens a dialog to import a single image."""
        if self.is_playing:
            QMessageBox.warning(self, "Playback Active", "Please pause playback to import an image.")
            return
            
        path, _ = QFileDialog.getOpenFileName(self, "Import Image", "", "Images (*.png *.jpg *.jpeg *.bmp)")
        if path:
            self.paste_image_on_current_frame(path)

    def set_image_folder(self):
        """Opens a dialog to select a folder of images for the randomizer."""
        dir_path = QFileDialog.getExistingDirectory(self, "Select Folder with Images")
        if dir_path:
            self.image_folder_path = dir_path
            self.image_file_list = []
            valid_extensions = ('.png', '.jpg', '.jpeg', '.bmp')
            
            try:
                for root, _, files in os.walk(dir_path):
                    for file in files:
                        if file.lower().endswith(valid_extensions):
                            self.image_file_list.append(os.path.join(root, file))
            except Exception as e:
                QMessageBox.critical(self, "Folder Error", f"Error scanning folder:\n{e}")
                return
            
            if self.image_file_list:
                self.throw_image_btn.setEnabled(True)
                self.folder_label.setText(f"{len(self.image_file_list)} images found.")
                self.statusBar().showMessage(f"Loaded {len(self.image_file_list)} images from {os.path.basename(dir_path)}", 5000)
            else:
                self.throw_image_btn.setEnabled(False)
                self.folder_label.setText("No valid images found in folder.")
                self.statusBar().showMessage("No valid images found in selected folder.", 5000)

    def throw_random_image(self):
        """Grabs a random image from the loaded list and pastes it."""
        if not self.image_file_list:
            QMessageBox.warning(self, "No Images", "Please set an image folder first using 'Set Image Folder...'.")
            return
            
        random_path = random.choice(self.image_file_list)
        self.paste_image_on_current_frame(random_path)

    # --- Audio Integration ---
    def stop_audio(self):
        """Stops the currently playing audio track."""
        if AUDIO_AVAILABLE:
            pygame.mixer.music.stop()
        self.is_audio_paused = False
        self.pause_audio_btn.setText("Pause Audio")
            
    def toggle_audio_pause(self):
        if not AUDIO_AVAILABLE or not self.current_audio_track:
            return

        if self.is_audio_paused:
            pygame.mixer.music.unpause()
            self.is_audio_paused = False
            self.pause_audio_btn.setText("Pause Audio")
        else:
            pygame.mixer.music.pause()
            self.is_audio_paused = True
            self.pause_audio_btn.setText("Resume Audio")
        
    def set_audio_folder(self):
        """Opens a dialog to select a folder of audio files."""
        if not AUDIO_AVAILABLE:
            QMessageBox.critical(self, "Audio Error", "Audio playback requires 'pygame'. Please run 'pip install pygame'.")
            return
            
        dir_path = QFileDialog.getExistingDirectory(self, "Select Folder with Audio Files")
        if dir_path:
            self.stop_audio()
            self.audio_folder_path = dir_path
            self.audio_file_list = []
            # Focusing on WAV for reliable playback via pygame
            valid_extensions = ('.wav',) 
            
            try:
                # Scan recursively
                for root, _, files in os.walk(dir_path):
                    for file in files:
                        if file.lower().endswith(valid_extensions):
                            self.audio_file_list.append(os.path.join(root, file))
            except Exception as e:
                QMessageBox.critical(self, "Folder Error", f"Error scanning audio folder:\n{e}")
                return
            
            if self.audio_file_list:
                self.next_audio_btn.setEnabled(True)
                self.pause_audio_btn.setEnabled(False)
                self.audio_folder_label.setText(f"{len(self.audio_file_list)} WAV tracks found.")
                self.current_audio_index = -1 # Start before the first track
                self.next_audio_track() # Load and play the first track
            else:
                self.next_audio_btn.setEnabled(False)
                self.pause_audio_btn.setEnabled(False)
                self.audio_folder_label.setText("No valid WAV audio files found.")

    def next_audio_track(self):
        """Loads and plays the next random audio track."""
        if not self.audio_file_list:
            QMessageBox.warning(self, "No Audio", "Please set an audio folder first.")
            return
            
        self.stop_audio()

        # Simple sequential/looping selection
        self.current_audio_index = (self.current_audio_index + 1) % len(self.audio_file_list)
        self.current_audio_track = self.audio_file_list[self.current_audio_index]
        
        # Display the loaded track name
        track_name = os.path.basename(self.current_audio_track)
        self.current_audio_display.setText(f"Playing: {track_name}")
        self.statusBar().showMessage(f"Audio loaded: {track_name}", 5000)

        # --- Actual Playback ---
        try:
            pygame.mixer.music.load(self.current_audio_track)
            pygame.mixer.music.play()
            self.pause_audio_btn.setEnabled(True)
            self.is_audio_paused = False
            self.pause_audio_btn.setText("Pause Audio")
        except Exception as e:
            QMessageBox.critical(self, "Playback Error", f"Could not play audio track:\n{e}\n(Ensure it is a valid WAV file)")
        
    # --- Exporting and Recording ---
    def export_video_file(self, format_type):
        """Exports all frames as a clean video file (offline - records timeline once)."""
        
        self.stop_audio()  # Stop audio before recording

        if self.current_audio_track and format_type == 'mp4':
            # Inform user that audio will be included (update the message)
            QMessageBox.information(self, "Audio Export Note", 
                f"The video will include the audio track:\n{os.path.basename(self.current_audio_track)}\n"
                "If the video duration doesn't match the audio, it may be trimmed or have silence."
            )
        elif self.current_audio_track:
            QMessageBox.information(self, "Audio Export Note", 
                "Audio will not be included (GIFs don't support audio, or no MP4 selected). "
                "Use a separate tool if needed."
            )

        if not self.frames:
            QMessageBox.warning(self, "Export Error", "No frames to export.")
            return

        file_filter = f"{format_type.upper()} (*.{format_type})"
        path, _ = QFileDialog.getSaveFileName(self, f"Export as {format_type.upper()}", "", file_filter)
        
        if not path:
            return

        try:
            self.recording_path = path
            fps = self.speed_slider.value()
            
            # Reset view for recording
            self.reset_view()
            
            # Create a white background for the output video
            output_frames = []
            for frame_pixmap in self.frames:
                bg_pixmap = QPixmap(self.canvas_size)
                bg_pixmap.fill(Qt.GlobalColor.white)
                painter = QPainter(bg_pixmap)
                painter.drawPixmap(0, 0, frame_pixmap)
                painter.end()
                output_frames.append(bg_pixmap)
            
            # Use imageio to write frames (video only)
            self.recording_writer = imageio.get_writer(self.recording_path, fps=fps, macro_block_size=1)
            
            for pixmap in output_frames:
                image = pixmap.toImage()
                numpy_image = qimage_to_numpy(image)
                self.recording_writer.append_data(numpy_image)
            
            self.recording_writer.close()
            self.recording_writer = None
            
            # If MP4 and audio is loaded, merge audio using ffmpeg
            if self.current_audio_track and format_type == 'mp4':
                self.merge_audio_to_mp4(path)
            
            QMessageBox.information(self, "Success", f"Animation successfully saved to {os.path.basename(self.recording_path)}")

        except Exception as e:
            if self.recording_writer:
                self.recording_writer.close()
                self.recording_writer = None
            QMessageBox.critical(self, "Recording Error", f"Could not save recording:\n{e}")

    def merge_audio_to_mp4(self, video_path):
        """Merges the current audio track into the MP4 video using ffmpeg."""
        try:
            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
            temp_path = video_path + '.temp.mp4'
            os.rename(video_path, temp_path)
            
            # Run ffmpeg to merge: copy video stream, encode audio to AAC
            # -shortest: trim to the shorter duration (video or audio)
            result = subprocess.run([
                ffmpeg_path,
                '-i', temp_path,               # Input video (no audio)
                '-i', self.current_audio_track, # Input audio (WAV)
                '-c:v', 'copy',                # Copy video stream (no re-encode)
                '-c:a', 'aac',                 # Encode audio to AAC (MP4 compatible)
                '-strict', 'experimental',     # Allow AAC
                '-shortest',                   # Trim to shorter duration
                video_path                     # Output
            ], capture_output=True, text=True)
            
            if result.returncode != 0:
                raise Exception(f"ffmpeg error: {result.stderr}")
            
            os.remove(temp_path)
            self.statusBar().showMessage("Audio successfully merged into MP4.", 5000)
        
        except Exception as e:
            QMessageBox.warning(self, "Audio Merge Warning", f"Could not merge audio:\n{e}\nThe video was saved without audio.")
            # Restore original video if merge failed
            if os.path.exists(temp_path):
                os.rename(temp_path, video_path)

    def start_live_recording(self, format_type):
        """Starts the live recording (captures playback loop until stopped)."""
        if self.is_recording:
            QMessageBox.warning(self, "Already Recording", "Already recording. Stop first.")
            return

        file_filter = f"{format_type.upper()} (*.{format_type})"
        path, _ = QFileDialog.getSaveFileName(self, f"Record Live as {format_type.upper()}", "", file_filter)
        
        if not path:
            return

        try:
            # FIX: REMOVED self.stop_audio() here so music plays during recording
            self.recording_path = path
            fps = self.speed_slider.value()
            self.recording_writer = imageio.get_writer(self.recording_path, fps=fps, macro_block_size=1)
            
            self.is_recording = True
            self.update_status()

            # Swap menu items to show "Stop Recording"
            self.live_record_gif_action.setVisible(False)
            self.live_record_mp4_action.setVisible(False)
            self.export_gif_action.setVisible(False)
            self.export_mp4_action.setVisible(False)
            self.export_seq_action.setVisible(False)
            self.export_menu.removeAction(self.stop_recording_action) # Ensure it's not duplicated
            self.export_menu.addAction(self.stop_recording_action)

            # FIX: Ensure playback starts for live recording
            if not self.is_playing:
                self.toggle_playback()

        except Exception as e:
            QMessageBox.critical(self, "Recording Error", f"Could not start recording:\n{e}")
            self.stop_recording() # Clean up if start fails

    def stop_recording(self):
        """
        Finalizes the live recording and cleans up state.
        """
        if not self.is_recording:
            return

        if self.recording_writer:
            self.recording_writer.close()
        
        saved_path = self.recording_path
        
        # If MP4 and audio is loaded, merge audio using ffmpeg
        ext = os.path.splitext(saved_path)[1].lower()
        if self.current_audio_track and ext == '.mp4':
            self.merge_audio_to_mp4(saved_path)
        
        self.is_recording = False
        self.recording_writer = None
        self.recording_path = None
        
        # Restore menu to original state
        self.export_menu.removeAction(self.stop_recording_action)
        self.live_record_gif_action.setVisible(True)
        self.live_record_mp4_action.setVisible(True)
        self.export_gif_action.setVisible(True)
        self.export_mp4_action.setVisible(True)
        self.export_seq_action.setVisible(True)

        self.update_status()
        if saved_path:
            QMessageBox.information(self, "Success", f"Animation successfully recorded to {os.path.basename(saved_path)}")

    def export_image_sequence(self):
        """Exports all frames as individual PNG images."""
        if self.is_recording:
            QMessageBox.warning(self, "Recording Active", "Please stop recording first.")
            return

        if not self.frames:
            QMessageBox.warning(self, "Export Error", "No frames to export.")
            return
            
        dir_path = QFileDialog.getExistingDirectory(self, "Select Directory for Image Sequence")
        if dir_path:
            try:
                for i, frame in enumerate(self.frames):
                    # Create a white-backed version for export
                    export_pixmap = QPixmap(self.canvas_size)
                    export_pixmap.fill(Qt.GlobalColor.white)
                    painter = QPainter(export_pixmap)
                    painter.drawPixmap(0, 0, frame)
                    painter.end()

                    frame_path = os.path.join(dir_path, f"frame_{i:04d}.png")
                    export_pixmap.save(frame_path, "PNG")
                    
                QMessageBox.information(self, "Success", f"Image sequence saved in {dir_path}")
            except Exception as e:
                QMessageBox.critical(self, "Export Error", f"An error occurred during export:\n{e}")
    
    def closeEvent(self, event):
        """Ensures recording is stopped and audio is stopped when closing the app."""
        if self.is_recording:
            self.stop_recording()
        self.stop_audio()
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = AnimatorApp()
    window.show()
    sys.exit(app.exec())