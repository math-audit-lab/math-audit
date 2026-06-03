#!/usr/bin/env bash

# Double-clickable macOS launcher for Math Paper Audit.
# It creates/uses the conda environment, runs the setup check, then starts the GUI.

set -u

ENV_NAME="math-audit"

pause_for_error() {
  echo
  echo "ERROR: $*"
  echo
  echo "Press Return to close this window."
  read -r _unused
  exit 1
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || pause_for_error "Could not open the Math Paper Audit folder."

find_env_tool() {
  local candidate

  if command -v conda >/dev/null 2>&1; then
    command -v conda
    return 0
  fi

  if command -v mamba >/dev/null 2>&1; then
    command -v mamba
    return 0
  fi

  for candidate in \
    "$HOME/miniforge3/bin/conda" \
    "$HOME/miniforge3/condabin/conda" \
    "$HOME/miniconda3/bin/conda" \
    "$HOME/miniconda3/condabin/conda" \
    "$HOME/anaconda3/bin/conda" \
    "$HOME/anaconda3/condabin/conda" \
    "$HOME/mambaforge/bin/conda" \
    "$HOME/mambaforge/condabin/conda" \
    "$HOME/miniforge3/bin/mamba" \
    "$HOME/miniconda3/bin/mamba" \
    "$HOME/anaconda3/bin/mamba" \
    "$HOME/mambaforge/bin/mamba"
  do
    if [ -x "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done

  return 1
}

CONDA_TOOL="$(find_env_tool || true)"

if [ -z "$CONDA_TOOL" ]; then
  cat <<'EOF'
Math Paper Audit needs Miniforge, Conda, or Mamba to create its Python environment.

Recommended macOS installer:
  https://github.com/conda-forge/miniforge/releases

Install Miniforge, then double-click run_math_audit.command again.
EOF
  pause_for_error "Conda/Mamba was not found."
fi

if [ ! -f "environment.yml" ]; then
  pause_for_error "environment.yml was not found. Make sure this launcher is inside the Math Paper Audit folder."
fi

echo "Math Paper Audit launcher"
echo "Project folder: $SCRIPT_DIR"
echo "Environment tool: $CONDA_TOOL"
echo

env_exists() {
  "$CONDA_TOOL" env list 2>/dev/null | awk '{print $1}' | grep -qx "$ENV_NAME"
}

if env_exists; then
  echo "Using existing '$ENV_NAME' environment."
else
  echo "Creating '$ENV_NAME' environment from environment.yml."
  echo "This may take several minutes the first time."
  echo
  if ! "$CONDA_TOOL" env create -f environment.yml; then
    cat <<'EOF'

Environment creation failed.

Common causes:
  - Miniforge/Conda installation is incomplete.
  - Internet access is unavailable.
  - The environment already exists but is damaged.
  - Package downloads were interrupted.

See QUICKSTART.md for manual setup instructions.
EOF
    pause_for_error "Could not create the '$ENV_NAME' environment."
  fi
fi

echo
echo "Running setup check..."
if ! "$CONDA_TOOL" run -n "$ENV_NAME" python scripts/check_setup.py; then
  pause_for_error "Setup check failed. See the messages above and QUICKSTART.md."
fi

echo
echo "Launching Math Paper Audit GUI..."
echo "Paste your OpenAI API key in the GUI when you are ready to use live audit/discussion actions."
echo

if ! "$CONDA_TOOL" run -n "$ENV_NAME" python audit_gui.py; then
  pause_for_error "The GUI exited with an error."
fi

echo
echo "Math Paper Audit has closed. You can close this Terminal window."
