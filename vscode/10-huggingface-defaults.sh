#!/bin/bash
# 10-huggingface-defaults.sh - Enable accelerated Hugging Face Hub transfers.

export HF_HUB_ENABLE_HF_TRANSFER="${HF_HUB_ENABLE_HF_TRANSFER:-1}"
