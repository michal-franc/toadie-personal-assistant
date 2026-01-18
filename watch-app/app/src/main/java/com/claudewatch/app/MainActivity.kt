package com.claudewatch.app

import android.Manifest
import android.content.pm.PackageManager
import android.media.MediaRecorder
import android.os.Bundle
import android.os.VibrationEffect
import android.os.Vibrator
import android.util.Log
import android.view.View
import android.widget.Button
import android.widget.ProgressBar
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import java.io.IOException

class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "ClaudeWatch"
        // TODO: Make this configurable
        private const val SERVER_URL = "http://192.168.1.100:5566/transcribe"
    }

    private lateinit var recordButton: Button
    private lateinit var statusText: TextView
    private lateinit var progressBar: ProgressBar

    private var mediaRecorder: MediaRecorder? = null
    private var audioFile: File? = null
    private var isRecording = false

    private val coroutineScope = CoroutineScope(Dispatchers.Main + Job())
    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        .writeTimeout(60, java.util.concurrent.TimeUnit.SECONDS)
        .readTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        .build()

    private val requestPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { isGranted ->
        if (isGranted) {
            toggleRecording()
        } else {
            showStatus("Microphone permission denied", isError = true)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        recordButton = findViewById(R.id.recordButton)
        statusText = findViewById(R.id.statusText)
        progressBar = findViewById(R.id.progressBar)

        recordButton.setOnClickListener {
            onRecordButtonClick()
        }

        showStatus("Tap to record")
    }

    override fun onDestroy() {
        super.onDestroy()
        coroutineScope.cancel()
        stopRecording()
        httpClient.dispatcher.executorService.shutdown()
    }

    private fun onRecordButtonClick() {
        if (checkPermission()) {
            toggleRecording()
        } else {
            requestPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    private fun checkPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
    }

    private fun toggleRecording() {
        if (isRecording) {
            stopRecordingAndSend()
        } else {
            startRecording()
        }
    }

    private fun startRecording() {
        try {
            // Create temp file for recording
            audioFile = File.createTempFile("recording_", ".m4a", cacheDir)

            mediaRecorder = MediaRecorder().apply {
                setAudioSource(MediaRecorder.AudioSource.MIC)
                setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
                setAudioEncodingBitRate(128000)
                setAudioSamplingRate(44100)
                setOutputFile(audioFile?.absolutePath)
                prepare()
                start()
            }

            isRecording = true
            vibrate(50)
            updateUI()
            showStatus("Recording...")
            Log.d(TAG, "Recording started: ${audioFile?.absolutePath}")

        } catch (e: Exception) {
            Log.e(TAG, "Failed to start recording", e)
            showStatus("Failed to start recording", isError = true)
            cleanupRecording()
        }
    }

    private fun stopRecordingAndSend() {
        stopRecording()
        vibrate(100)
        sendRecording()
    }

    private fun stopRecording() {
        try {
            mediaRecorder?.apply {
                stop()
                release()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error stopping recording", e)
        }
        mediaRecorder = null
        isRecording = false
        updateUI()
    }

    private fun sendRecording() {
        val file = audioFile ?: run {
            showStatus("No recording to send", isError = true)
            return
        }

        showStatus("Sending...")
        progressBar.visibility = View.VISIBLE
        recordButton.isEnabled = false

        coroutineScope.launch {
            try {
                val result = withContext(Dispatchers.IO) {
                    sendToServer(file)
                }

                if (result.isSuccess) {
                    showStatus("Sent successfully!")
                    vibrate(50)
                    // Cleanup the file after successful send
                    file.delete()
                    audioFile = null
                } else {
                    showStatus("Failed: ${result.exceptionOrNull()?.message}", isError = true)
                    vibrate(longArrayOf(0, 100, 100, 100))
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error sending recording", e)
                showStatus("Error: ${e.message}", isError = true)
                vibrate(longArrayOf(0, 100, 100, 100))
            } finally {
                progressBar.visibility = View.GONE
                recordButton.isEnabled = true
            }
        }
    }

    private suspend fun sendToServer(file: File): Result<String> {
        return try {
            val requestBody = file.asRequestBody("audio/mp4".toMediaType())

            val request = Request.Builder()
                .url(SERVER_URL)
                .post(requestBody)
                .build()

            val response = httpClient.newCall(request).execute()

            if (response.isSuccessful) {
                val body = response.body?.string() ?: ""
                Log.d(TAG, "Server response: $body")
                Result.success(body)
            } else {
                val error = "Server error: ${response.code}"
                Log.e(TAG, error)
                Result.failure(IOException(error))
            }
        } catch (e: Exception) {
            Log.e(TAG, "Network error", e)
            Result.failure(e)
        }
    }

    private fun cleanupRecording() {
        mediaRecorder?.release()
        mediaRecorder = null
        audioFile?.delete()
        audioFile = null
        isRecording = false
        updateUI()
    }

    private fun updateUI() {
        recordButton.text = if (isRecording) "Stop & Send" else "Record"
        recordButton.setBackgroundColor(
            if (isRecording)
                ContextCompat.getColor(this, android.R.color.holo_red_dark)
            else
                ContextCompat.getColor(this, android.R.color.holo_blue_dark)
        )
    }

    private fun showStatus(message: String, isError: Boolean = false) {
        statusText.text = message
        statusText.setTextColor(
            if (isError)
                ContextCompat.getColor(this, android.R.color.holo_red_light)
            else
                ContextCompat.getColor(this, android.R.color.white)
        )
    }

    private fun vibrate(duration: Long) {
        val vibrator = getSystemService(Vibrator::class.java)
        vibrator?.vibrate(VibrationEffect.createOneShot(duration, VibrationEffect.DEFAULT_AMPLITUDE))
    }

    private fun vibrate(pattern: LongArray) {
        val vibrator = getSystemService(Vibrator::class.java)
        vibrator?.vibrate(VibrationEffect.createWaveform(pattern, -1))
    }
}
