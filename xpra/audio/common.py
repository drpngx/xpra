#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2016-2023 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from xpra.util import csv, engs
from xpra.log import Logger
log = Logger("audio")


VORBIS = "vorbis"
AAC = "aac"
FLAC = "flac"
MP3 = "mp3"
WAV = "wav"
OPUS = "opus"
SPEEX = "speex"
WAVPACK = "wavpack"

OGG = "ogg"
MKA = "mka"
MPEG4 = "mpeg4"
ID3V2 = "id3v2"
#RTP = "rtp"
RAW = "raw"

#stream compression
LZ4 = "lz4"
#LZO = "lzo"

FLAC_OGG    = FLAC+"+"+OGG
OPUS_OGG    = OPUS+"+"+OGG
SPEEX_OGG   = SPEEX+"+"+OGG
VORBIS_OGG  = VORBIS+"+"+OGG
OPUS_MKA    = OPUS+"+"+MKA
#OPUS_RTP    = OPUS+"+"+RTP
VORBIS_MKA  = VORBIS+"+"+MKA
AAC_MPEG4   = AAC+"+"+MPEG4
WAV_LZ4     = WAV+"+"+LZ4
#WAV_LZO     = WAV+"+"+LZO
MP3_MPEG4   = MP3+"+"+MPEG4
MP3_ID3V2   = MP3+"+"+ID3V2


#used for parsing codec names specified on the command line:
def audio_option_or_all(name:str, options, all_values:tuple[str,...]) -> tuple[str,...]:
    log("audio_option_or_all%s", (name, options, all_values))
    if not options:
        v = list(all_values)              #not specified on command line: use default
    else:
        v = []
        invalid_options = []
        for x in options:
            #options is a list, but it may have csv embedded:
            for o in x.split(","):
                o = o.strip()
                if o not in all_values:
                    invalid_options.append(o)
                else:
                    v.append(o)
        if invalid_options:
            if all_values:
                log.warn("Warning: invalid value%s for '%s': %s", engs(invalid_options), name, csv(invalid_options))
                log.warn(" valid option%s: %s", engs(all_values), csv(all_values))
            else:
                log.warn("Warning: no '%ss' available", name)
    log("%s=%s", name, csv(v))
    return tuple(v)
