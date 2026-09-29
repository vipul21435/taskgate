#!/bin/sh
# Reference solution: awk splits each record on runs of blanks, so NF is the
# word count (0 for an empty line).
set -eu
mkdir -p output
awk '{ print NF }' input/text.txt > output/counts.txt
