package com.claudewatch.companion

import android.content.Context
import android.content.SharedPreferences
import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.claudewatch.companion.databinding.ActivitySettingsBinding

class SettingsActivity : AppCompatActivity() {

    companion object {
        private const val PREFS_NAME = "claude_companion_prefs"
        private const val KEY_SERVER_ADDRESS = "server_address"
        private const val KEY_KIOSK_MODE = "kiosk_mode"
        private const val KEY_AUTO_RETRY = "auto_retry"
        private const val DEFAULT_SERVER = "192.168.1.100:5567"

        fun getServerAddress(context: Context): String {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            return prefs.getString(KEY_SERVER_ADDRESS, DEFAULT_SERVER) ?: DEFAULT_SERVER
        }

        fun isKioskModeEnabled(context: Context): Boolean {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            return prefs.getBoolean(KEY_KIOSK_MODE, false)
        }

        fun isAutoRetryEnabled(context: Context): Boolean {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            return prefs.getBoolean(KEY_AUTO_RETRY, false)
        }
    }

    private lateinit var binding: ActivitySettingsBinding
    private lateinit var prefs: SharedPreferences

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setSupportActionBar(binding.toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

        // Load current settings
        binding.serverAddressInput.setText(getServerAddress(this))
        binding.kioskModeSwitch.isChecked = isKioskModeEnabled(this)
        binding.autoRetrySwitch.isChecked = isAutoRetryEnabled(this)

        // Save button
        binding.saveButton.setOnClickListener {
            saveSettings()
        }
    }

    private fun saveSettings() {
        val serverAddress = binding.serverAddressInput.text.toString().trim()
        val kioskMode = binding.kioskModeSwitch.isChecked
        val autoRetry = binding.autoRetrySwitch.isChecked

        if (serverAddress.isEmpty()) {
            binding.serverAddressInput.error = "Server address is required"
            return
        }

        prefs.edit()
            .putString(KEY_SERVER_ADDRESS, serverAddress)
            .putBoolean(KEY_KIOSK_MODE, kioskMode)
            .putBoolean(KEY_AUTO_RETRY, autoRetry)
            .apply()

        Toast.makeText(this, "Settings saved", Toast.LENGTH_SHORT).show()
        finish()
    }

    override fun onSupportNavigateUp(): Boolean {
        onBackPressedDispatcher.onBackPressed()
        return true
    }
}
