"""Color constants and stylesheets for the AI REFs GUI."""

COLOR_HIGH = "#27ae60"        # Green
COLOR_MEDIUM = "#f39c12"      # Yellow/Orange
COLOR_LOW = "#e74c3c"         # Red
COLOR_UNRESOLVED = "#95a5a6"  # Grey
COLOR_ACCEPTED = "#2980b9"    # Blue
COLOR_SKIPPED = "#7f8c8d"     # Dark grey: marker left unchanged by the user

MAIN_STYLESHEET = """
QMainWindow {
    background-color: #f5f6fa;
}
QTabWidget::pane {
    border: 1px solid #dcdde1;
    background: white;
}
QTabBar::tab {
    background: #e8e8e8;
    padding: 8px 24px;
    margin-right: 2px;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    font-size: 13px;
}
QTabBar::tab:selected {
    background: white;
    border-bottom: 2px solid #4a90d9;
    font-weight: bold;
}
QLabel#sectionHeader {
    font-size: 18px;
    font-weight: bold;
    color: #2c3e50;
}
QLabel#subHeader {
    font-size: 14px;
    font-weight: bold;
    color: #555;
}
QPushButton#primaryButton {
    background-color: #4a90d9;
    color: white;
    padding: 8px 24px;
    border-radius: 4px;
    font-size: 13px;
    font-weight: bold;
}
QPushButton#primaryButton:hover {
    background-color: #357abd;
}
QPushButton#successButton {
    background-color: #27ae60;
    color: white;
    padding: 8px 24px;
    border-radius: 4px;
    font-size: 13px;
    font-weight: bold;
}
QPushButton#successButton:hover {
    background-color: #219a52;
}
QTextEdit#logPanel {
    background-color: #1e1e2e;
    color: #d4d4d4;
    font-family: "Courier New", monospace;
    font-size: 12px;
    border: 1px solid #444;
    border-radius: 4px;
}
QGroupBox {
    font-weight: bold;
    border: 1px solid #ddd;
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 16px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
}
"""
