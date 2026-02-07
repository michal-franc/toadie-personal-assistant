package com.claudewatch.app

import android.app.Activity
import android.content.Intent
import android.os.Bundle

/**
 * Trampoline activity that launches MainActivity with auto-record enabled.
 * Users can assign this to a hardware button double-press via
 * Settings -> Advanced features -> Customize buttons -> "Claude Record".
 */
class RecordActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val intent = Intent(this, MainActivity::class.java).apply {
            putExtra("auto_record", true)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        }
        startActivity(intent)
        finish()
    }
}
