package com.claudewatch.companion.creature

enum class CreatureMood {
    NEUTRAL,
    HAPPY,
    CURIOUS,
    FOCUSED,
    PROUD,
    CONFUSED,
    PLAYFUL;

    companion object {
        fun fromString(value: String): CreatureMood =
            entries.firstOrNull { it.name.equals(value, ignoreCase = true) } ?: NEUTRAL
    }
}

enum class BackgroundTheme {
    DEFAULT,
    WARM,
    COOL,
    NATURE,
    ELECTRIC;

    companion object {
        fun fromString(value: String): BackgroundTheme =
            entries.firstOrNull { it.name.equals(value, ignoreCase = true) } ?: DEFAULT
    }
}
