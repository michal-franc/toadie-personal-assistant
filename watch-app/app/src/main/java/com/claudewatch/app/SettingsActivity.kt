package com.claudewatch.app

import android.content.Context
import android.content.SharedPreferences
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.Toast
import android.app.Activity

class SettingsActivity : Activity() {

    companion object {
        private const val PREFS_NAME = "ClaudeWatchPrefs"
        private const val KEY_SERVER_IP = "server_ip"
        private const val KEY_SERVER_PORT = "server_port"
        private const val DEFAULT_IP = "192.168.1.100"
        private const val DEFAULT_PORT = "5566"

        fun getServerUrl(context: Context): String {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            val ip = prefs.getString(KEY_SERVER_IP, DEFAULT_IP) ?: DEFAULT_IP
            val port = prefs.getString(KEY_SERVER_PORT, DEFAULT_PORT) ?: DEFAULT_PORT
            return "http://$ip:$port/transcribe"
        }
    }

    private lateinit var ipEditText: EditText
    private lateinit var portEditText: EditText
    private lateinit var saveButton: Button
    private lateinit var prefs: SharedPreferences

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)

        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

        ipEditText = findViewById(R.id.ipEditText)
        portEditText = findViewById(R.id.portEditText)
        saveButton = findViewById(R.id.saveButton)

        // Load saved values
        ipEditText.setText(prefs.getString(KEY_SERVER_IP, DEFAULT_IP))
        portEditText.setText(prefs.getString(KEY_SERVER_PORT, DEFAULT_PORT))

        saveButton.setOnClickListener {
            saveSettings()
        }
    }

    private fun saveSettings() {
        val ip = ipEditText.text.toString().trim()
        val port = portEditText.text.toString().trim()

        if (ip.isEmpty()) {
            Toast.makeText(this, "IP address required", Toast.LENGTH_SHORT).show()
            return
        }

        if (port.isEmpty()) {
            Toast.makeText(this, "Port required", Toast.LENGTH_SHORT).show()
            return
        }

        prefs.edit()
            .putString(KEY_SERVER_IP, ip)
            .putString(KEY_SERVER_PORT, port)
            .apply()

        Toast.makeText(this, "Settings saved", Toast.LENGTH_SHORT).show()
        finish()
    }
}
