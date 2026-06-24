# Smart Transfer

**Smart Transfer** is a modern folder copy and zip archiving tool built with Python and CustomTkinter. It is designed specifically for developers who need to transfer or backup large projects without dragging along gigabytes of build artifacts (like `node_modules`, `.venv`, `target`, etc.).

## ✨ Features

- **Smart Exclusions**: Skip unnecessary files and folders to save time and disk space.
- **Built-in Presets**: Comes with predefined exclusion lists for popular tech stacks:
  - MERN / Node.js
  - React Native
  - Python
  - Flutter
  - Java / Maven
- **Custom Presets**: Create, manage, and save your own exclusion rules.
- **Analyze Tool**: Scan a directory before transferring to preview what will be copied, see the estimated size, and detect unusually large folders.
- **Dual Transfer Modes**:
  - **Copy Folder**: Transfer the filtered project to a new directory.
  - **Create ZIP**: Compress the filtered project directly into a ZIP archive.
- **Progress Tracking**: Real-time progress bars, detailed statistics, and post-transfer reports (which can be exported to text).
- **Modern UI**: Professional dark-themed interface built using `customtkinter`.

## ⚙️ Requirements

- Python 3.10+
- `customtkinter`

## 🚀 Installation & Usage

1. Install the required dependency:
   ```bash
   pip install customtkinter
   ```

2. Run the application:
   ```bash
   python smartTransfer.py
   ```

## 🛠️ How it Works

1. **Select Source**: Choose the project folder you want to transfer.
2. **Set Exclusions**: Pick a preset (e.g., Python, Node.js) or manually type out folders/files to exclude.
3. **Select Destination**: Choose a target folder or a destination ZIP file path.
4. **Analyze (Optional but Recommended)**: Click `Analyze` to see statistics and detect large folders that you might want to add to your exclusions.
5. **Transfer**: Click `Copy Folder` or `Create ZIP` to start the process.

## 📄 Technical Details

- **I/O Operations**: Uses Python's built-in `shutil`, `zipfile`, and `pathlib` for efficient file operations.
- **Threading**: Implements a queue-based multi-threading system for background scanning and transfers to ensure the UI remains fully responsive.
- **Configuration**: User settings and custom presets are automatically saved to `~/.smart_transfer_config.json`.
