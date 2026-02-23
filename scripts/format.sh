#!/bin/bash
set -e

# Format checker/fixer script for NGM project

if [ "$1" = "--check" ]; then
    echo "Checking code formatting..."
    poetry run black --check .
    echo "Checking linting..."
    poetry run ruff check .
    echo "All checks passed!"
else
    echo "Formatting code with black..."
    poetry run black .
    echo "Fixing linting issues with ruff..."
    poetry run ruff check --fix .
    echo "Formatting complete!"
fi
