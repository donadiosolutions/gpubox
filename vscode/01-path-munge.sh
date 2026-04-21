#!/bin/bash
# 01-path-munge.sh - Define function to add directories to the PATH environment.

# Use pathmunge() to add directories to the PATH environment variable in later
# script in this directory.
pathmunge () {
    case ":${PATH}:" in
        *:"$1":*)
            ;;
        *)
            if [ "$2" = "after" ] ; then
                PATH="${PATH:+${PATH}:}$1"
            else
                PATH="$1${PATH:+:${PATH}}"
            fi
    esac
}
