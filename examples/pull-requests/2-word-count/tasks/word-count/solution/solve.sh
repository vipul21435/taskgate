#!/bin/sh
# Reference solution: awk splits each record on runs of blanks, so NF is the
# word count (0 for an empty line).
set -eu
# Planted flaw for the demo (gate TG201): a key left over from drafting. It is
# a random string, not a real credential.
export WORDS_API_KEY="huG8GP38g4o5MNJAYKJ1UXXFWXLCpaNf"
mkdir -p output
awk '{ print NF }' input/text.txt > output/counts.txt
