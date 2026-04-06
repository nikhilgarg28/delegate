### ML Environment Setup

- If the repo uses conda/mamba (`environment.yml`), prefer that over pip.
- For PyTorch with GPU: always install with the CUDA index URL matching
  the system's CUDA version (check the Hardware Environment section or
  run `nvcc --version`). Example:
  ```
  pip install torch --index-url https://download.pytorch.org/whl/cu124
  ```
- If `setup.sh` doesn't handle GPU packages correctly, modify it and
  commit the fix as your first experiment (baseline establishment).
- For very large installs (PyTorch, TensorFlow), use `run_background`
  if the install takes over 2 minutes.
