"""Nomes de PIDs, tabelas, tipos de stream e descritores (MPEG, DVB, ISDB-Tb / ABNT)."""

NULL_PID = 0x1FFF
IIP_PID = 0x1FF0

# PIDs fixos (ISO 13818-1, ARIB STD-B10 / ABNT NBR 15603)
WELL_KNOWN_PIDS = {
    0x0000: "PAT",
    0x0001: "CAT",
    0x0002: "TSDT",
    0x0010: "NIT",
    0x0011: "SDT/BAT",
    0x0012: "EIT (H-EIT)",
    0x0013: "RST",
    0x0014: "TDT/TOT",
    0x0017: "DCT",
    0x001E: "DIT",
    0x001F: "SIT",
    0x0020: "LIT",
    0x0021: "ERT",
    0x0022: "PCAT",
    0x0023: "SDTT",
    0x0024: "BIT",
    0x0025: "NBIT/LDT",
    0x0026: "EIT (M-EIT)",
    0x0027: "EIT (L-EIT 1-seg)",
    0x0028: "SDTT",
    0x0029: "CDT",
    0x1FF0: "IIP (ISDB-T)",
    0x1FFF: "Null",
}

# PIDs que transportam seções PSI/SI por definição
SI_PIDS = {0x00, 0x01, 0x02, 0x10, 0x11, 0x12, 0x13, 0x14, 0x17, 0x1E, 0x1F,
           0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29}

TABLE_NAMES = {
    0x00: "PAT", 0x01: "CAT", 0x02: "PMT", 0x03: "TSDT",
    0x3A: "DSM-CC MPE", 0x3B: "DSM-CC DII/DSI", 0x3C: "DSM-CC DDB",
    0x3D: "DSM-CC Stream Descriptors", 0x3E: "DSM-CC Private", 0x3F: "DSM-CC Addressable",
    0x40: "NIT-actual", 0x41: "NIT-other", 0x42: "SDT-actual", 0x46: "SDT-other",
    0x4A: "BAT", 0x4E: "EIT-p/f actual", 0x4F: "EIT-p/f other",
    0x70: "TDT", 0x71: "RST", 0x72: "ST", 0x73: "TOT", 0x74: "AIT",
    0x7E: "DIT", 0x7F: "SIT",
    0xC0: "DCT", 0xC1: "DLT", 0xC2: "PCAT", 0xC3: "SDTT", 0xC4: "BIT",
    0xC5: "NBIT (corpo)", 0xC6: "NBIT (referência)", 0xC7: "LDT", 0xC8: "CDT",
    0xD0: "LIT", 0xD1: "ERT", 0xD2: "ITT", 0xFE: "AMT",
}
for _t in range(0x50, 0x60):
    TABLE_NAMES[_t] = "EIT-schedule actual"
for _t in range(0x60, 0x70):
    TABLE_NAMES[_t] = "EIT-schedule other"


def table_name(tid):
    return TABLE_NAMES.get(tid, "Tabela 0x%02X" % tid)


STREAM_TYPES = {
    0x00: "Reservado",
    0x01: "MPEG-1 Video",
    0x02: "MPEG-2 Video",
    0x03: "MPEG-1 Audio",
    0x04: "MPEG-2 Audio",
    0x05: "Private Sections",
    0x06: "PES Private Data",
    0x07: "MHEG",
    0x08: "DSM-CC",
    0x09: "H.222.1",
    0x0A: "13818-6 type A",
    0x0B: "13818-6 type B",
    0x0C: "13818-6 type C",
    0x0D: "13818-6 type D",
    0x0E: "13818-1 Auxiliary",
    0x0F: "AAC Audio (ADTS)",
    0x10: "MPEG-4 Video",
    0x11: "HE-AAC Audio",          # MPEG-4 AAC LATM (ISDB-Tb)
    0x12: "MPEG-4 SL PES",
    0x13: "MPEG-4 SL Sections",
    0x14: "13818-6 Sync Download",
    0x15: "Metadata PES",
    0x1B: "AVC/H.264 Video",
    0x1C: "MPEG-4 Audio",
    0x1F: "SVC Video",
    0x20: "MVC Video",
    0x24: "HEVC/H.265 Video",
    0x2D: "MPEG-H 3D Audio",
    0x2E: "MPEG-H 3D Audio aux",
    0x32: "JPEG XS Video",
    0x33: "VVC/H.266 Video",
    0x81: "AC-3 Audio",
    0x87: "E-AC-3 Audio",
}

VIDEO_STREAM_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x1F, 0x20, 0x24, 0x33}
AUDIO_STREAM_TYPES = {0x03, 0x04, 0x0F, 0x11, 0x1C, 0x2D, 0x81, 0x87}
SECTION_STREAM_TYPES = {0x05, 0x0A, 0x0B, 0x0C, 0x0D}


def stream_type_name(st):
    return STREAM_TYPES.get(st, "Stream type 0x%02X" % st)


def component_tag_kind(tag):
    """Classificação ISDB-Tb (ABNT NBR 15608 / ARIB TR-B14) pelo component_tag."""
    if tag is None:
        return None
    if 0x00 <= tag <= 0x0F:
        return "Vídeo"
    if 0x10 <= tag <= 0x2F:
        return "Áudio"
    if 0x30 <= tag <= 0x37:
        return "Closed Caption"
    if 0x38 <= tag <= 0x3F:
        return "Superimpose"
    if 0x40 <= tag <= 0x7F:
        return "Dados"
    if tag == 0x80:
        return "Dados (1-seg)"
    if tag == 0x81:
        return "Vídeo 1-seg"
    if tag == 0x83 or tag == 0x84:
        return "Áudio 1-seg"
    if tag == 0x87:
        return "Closed Caption 1-seg"
    if 0x80 <= tag <= 0x8F:
        return "1-seg"
    return None


DESCRIPTOR_NAMES = {
    0x02: "video_stream", 0x03: "audio_stream", 0x04: "hierarchy", 0x05: "registration",
    0x06: "data_stream_alignment", 0x07: "target_background_grid", 0x08: "video_window",
    0x09: "CA", 0x0A: "ISO_639_language", 0x0B: "system_clock", 0x0C: "multiplex_buffer_utilization",
    0x0D: "copyright", 0x0E: "maximum_bitrate", 0x0F: "private_data_indicator",
    0x10: "smoothing_buffer", 0x11: "STD", 0x12: "IBP", 0x13: "carousel_identifier",
    0x14: "association_tag", 0x15: "deferred_association_tags", 0x17: "NPT_reference",
    0x18: "NPT_endpoint", 0x19: "stream_mode", 0x1A: "stream_event", 0x1B: "MPEG-4_video",
    0x1C: "MPEG-4_audio", 0x1D: "IOD", 0x1E: "SL", 0x1F: "FMC", 0x20: "external_ES_ID",
    0x21: "MuxCode", 0x22: "FmxBufferSize", 0x23: "multiplexBuffer", 0x24: "content_labeling",
    0x25: "metadata_pointer", 0x26: "metadata", 0x27: "metadata_STD", 0x28: "AVC_video",
    0x29: "IPMP", 0x2A: "AVC_timing_and_HRD", 0x2B: "MPEG-2_AAC_audio", 0x2C: "FlexMuxTiming",
    0x2D: "MPEG-4_text", 0x2E: "MPEG-4_audio_extension", 0x2F: "auxiliary_video_stream",
    0x30: "SVC_extension", 0x31: "MVC_extension", 0x32: "J2K_video", 0x33: "MVC_operation_point",
    0x38: "HEVC_video", 0x39: "VVC_video", 0x3F: "MPEG_extension",
    0x40: "network_name", 0x41: "service_list", 0x42: "stuffing", 0x43: "satellite_delivery_system",
    0x44: "cable_delivery_system", 0x45: "VBI_data", 0x46: "VBI_teletext", 0x47: "bouquet_name",
    0x48: "service", 0x49: "country_availability", 0x4A: "linkage", 0x4B: "NVOD_reference",
    0x4C: "time_shifted_service", 0x4D: "short_event", 0x4E: "extended_event",
    0x4F: "time_shifted_event", 0x50: "component", 0x51: "mosaic", 0x52: "stream_identifier",
    0x53: "CA_identifier", 0x54: "content", 0x55: "parental_rating", 0x56: "teletext",
    0x57: "telephone", 0x58: "local_time_offset", 0x59: "subtitling",
    0x5A: "terrestrial_delivery_system", 0x5B: "multilingual_network_name",
    0x5C: "multilingual_bouquet_name", 0x5D: "multilingual_service_name",
    0x5E: "multilingual_component", 0x5F: "private_data_specifier", 0x60: "service_move",
    0x61: "short_smoothing_buffer", 0x62: "frequency_list", 0x63: "partial_transport_stream",
    0x64: "data_broadcast", 0x65: "scrambling", 0x66: "data_broadcast_id",
    0x67: "material_information (ISDB)", 0x68: "hybrid_information (ISDB)",
    0x6A: "AC-3", 0x6F: "application_signalling", 0x7A: "enhanced_AC-3", 0x7C: "AAC",
    0x7F: "DVB_extension",
    0xC0: "hierarchical_transmission", 0xC1: "digital_copy_control", 0xC2: "network_identification",
    0xC3: "partial_transport_stream_time", 0xC4: "audio_component", 0xC5: "hyperlink",
    0xC6: "target_region", 0xC7: "data_content", 0xC8: "video_decode_control",
    0xC9: "download_content", 0xCA: "CA_EMM_TS", 0xCB: "CA_contract_info", 0xCC: "CA_service",
    0xCD: "TS_information", 0xCE: "extended_broadcaster", 0xCF: "logo_transmission",
    0xD0: "basic_local_event", 0xD1: "reference", 0xD2: "node_relation",
    0xD3: "short_node_information", 0xD4: "STC_reference", 0xD5: "series", 0xD6: "event_group",
    0xD7: "SI_parameter", 0xD8: "broadcaster_name", 0xD9: "component_group",
    0xDA: "SI_prime_TS", 0xDB: "board_information", 0xDC: "LDT_linkage",
    0xDD: "connected_transmission", 0xDE: "content_availability", 0xDF: "ISDB_extension",
    0xE0: "service_group", 0xE1: "area_broadcasting_information", 0xE2: "network_download_content",
    0xE3: "download_protection", 0xE4: "CA_startup", 0xE5: "character_code",
    0xF3: "wired_multicarrier_transmission", 0xF4: "advanced_wired_delivery",
    0xF5: "scrambler", 0xF6: "access_control", 0xF7: "carousel_compatible_composite",
    0xF8: "conditional_playback", 0xF9: "cable_TS_division_system",
    0xFA: "terrestrial_delivery_system (ISDB)", 0xFB: "partial_reception",
    0xFC: "emergency_information", 0xFD: "data_component", 0xFE: "system_management",
}


def descriptor_name(tag):
    return DESCRIPTOR_NAMES.get(tag, "descriptor 0x%02X" % tag)


SERVICE_TYPES = {
    0x01: "TV digital", 0x02: "Rádio digital", 0x03: "Teletexto", 0x0C: "Dados",
    0x11: "TV MPEG-2 HD", 0x16: "TV H.264 SD", 0x19: "TV H.264 HD", 0x1F: "TV HEVC",
    0xA0: "Vídeo temporário", 0xA1: "Áudio temporário", 0xA2: "Dados temporários",
    0xA3: "Engenharia", 0xA4: "Promocional", 0xA5: "Promocional (áudio)",
    0xA6: "Promocional (dados)", 0xA7: "Dados de armazenamento antecipado",
    0xA8: "Dados exclusivos de armazenamento", 0xA9: "Lista de serviços de bookmark",
    0xAA: "Simultâneo tipo servidor", 0xAB: "Independente de arquivo", 0xC0: "1-seg / dados (0xC0)",
}

RUNNING_STATUS = {0: "indefinido", 1: "não está no ar", 2: "inicia em segundos",
                  3: "pausado", 4: "no ar", 5: "fora do ar"}

# Gêneros ISDB-Tb (ABNT NBR 15603-2, content_descriptor)
GENRES_BR = {
    0x0: "Jornalismo", 0x1: "Esporte", 0x2: "Educativo", 0x3: "Novela",
    0x4: "Minissérie", 0x5: "Série/seriado", 0x6: "Variedade", 0x7: "Reality show",
    0x8: "Informação", 0x9: "Humorístico", 0xA: "Infantil", 0xB: "Erótico",
    0xC: "Filme", 0xD: "Sorteio/televendas/premiação", 0xE: "Debate/entrevista",
    0xF: "Outros",
}

# Classificação indicativa (ABNT NBR 15603-2, parental_rating_descriptor, país BRA)
RATING_BR = {0x1: "L (livre)", 0x2: "10 anos", 0x3: "12 anos", 0x4: "14 anos",
             0x5: "16 anos", 0x6: "18 anos"}

# ISDB-T (ARIB STD-B31 / ABNT NBR 15601)
TMCC_IDENTIFIER = {0: "BS digital", 1: "reservado", 2: "TV digital terrestre",
                   3: "Áudio digital terrestre"}
LAYER_NAMES = {0: "Nulo", 1: "A", 2: "B", 3: "C", 4: "AC", 8: "IIP"}
LAYER_LONG = {0: "TSP nulo (não transmitido)", 1: "Camada A", 2: "Camada B", 3: "Camada C",
              4: "AC data (sem camada)", 8: "IIP (sem camada)"}
ISDB_MODES = {0: "reservado", 1: "Modo 1 (2k)", 2: "Modo 2 (4k)", 3: "Modo 3 (8k)"}
ISDB_GUARD = {0: "1/32", 1: "1/16", 2: "1/8", 3: "1/4"}
ISDB_GUARD_VAL = {0: 1 / 32, 1: 1 / 16, 2: 1 / 8, 3: 1 / 4}
ISDB_MODULATION = {0: "DQPSK", 1: "QPSK", 2: "16-QAM", 3: "64-QAM", 7: "não usada"}
ISDB_MOD_BITS = {0: 2, 1: 2, 2: 4, 3: 6}
ISDB_CODERATE = {0: "1/2", 1: "2/3", 2: "3/4", 3: "5/6", 4: "7/8", 7: "não usada"}
ISDB_CODERATE_VAL = {0: 1 / 2, 1: 2 / 3, 2: 3 / 4, 3: 5 / 6, 4: 7 / 8}
# Profundidade do entrelaçamento temporal (I) por código e modo
ISDB_TIME_INTERLEAVE = {
    0: {1: 0, 2: 0, 3: 0},
    1: {1: 4, 2: 2, 3: 1},
    2: {1: 8, 2: 4, 3: 2},
    3: {1: 16, 2: 8, 3: 4},
}
ISDB_SYSTEM_ID = {0: "TV digital terrestre", 1: "Áudio digital terrestre", 2: "reservado", 3: "reservado"}

VIDEO_ENCODE_FORMAT = {0: "1080p", 1: "1080i", 2: "720p", 3: "480p", 4: "480i",
                       5: "240p", 6: "120p", 7: "2160p"}

PID_KIND_ICON = {
    "psi": "table", "video": "video", "audio": "audio", "pcr": "clock",
    "data": "data", "null": "null", "iip": "isdb", "unknown": "unknown",
}
