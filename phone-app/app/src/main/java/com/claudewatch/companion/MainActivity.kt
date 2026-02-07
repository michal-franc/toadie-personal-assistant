package com.claudewatch.companion

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.graphics.drawable.ClipDrawable
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.LayerDrawable
import android.media.MediaRecorder
import android.os.Bundle
import android.util.Log
import android.view.MotionEvent
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import com.claudewatch.companion.chat.ChatAdapter
import com.claudewatch.companion.creature.CreatureMood
import com.claudewatch.companion.creature.CreatureState
import com.claudewatch.companion.databinding.ActivityMainBinding
import com.claudewatch.companion.kiosk.KioskManager
import com.claudewatch.companion.network.ChatMessage
import com.claudewatch.companion.network.ClaudePrompt
import com.claudewatch.companion.network.ConnectionStatus
import com.claudewatch.companion.network.ContextUsage
import com.claudewatch.companion.network.MessageStatus
import com.claudewatch.companion.network.WebSocketClient
import android.widget.LinearLayout
import android.widget.TextView
import android.graphics.Typeface
import com.claudewatch.companion.wakeword.WakeWordService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit

class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "MainActivity"
        private const val PERMISSION_REQUEST_CODE = 1001
        private const val NOTIFICATION_PERMISSION_CODE = 1002
    }

    private lateinit var binding: ActivityMainBinding
    private lateinit var chatAdapter: ChatAdapter
    private lateinit var kioskManager: KioskManager
    private var webSocketClient: WebSocketClient? = null
    private var idleTimeout: Long = 0

    // Pending messages queue (messages waiting to be sent)
    private val pendingMessages = MutableStateFlow<List<ChatMessage>>(emptyList())

    // Voice recording (toggle: tap to start, tap to stop)
    private var mediaRecorder: MediaRecorder? = null
    private var audioFile: File? = null
    private var isRecording = false

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        kioskManager = KioskManager(this)

        setupChatRecyclerView()
        setupClickListeners()
        setupInputHandlers()
        connectWebSocket()

        // Enter kiosk mode if enabled in settings
        if (SettingsActivity.isKioskModeEnabled(this)) {
            enterKioskMode()
        }

        // Request notification permission on API 33+ (needed for foreground service)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(
                    this,
                    arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                    NOTIFICATION_PERMISSION_CODE
                )
            }
        }

        // Auto-start wake word service if enabled
        if (SettingsActivity.isWakeWordEnabled(this)) {
            WakeWordService.start(this)
        }
    }

    private fun setupChatRecyclerView() {
        chatAdapter = ChatAdapter { message ->
            // Retry sending failed/pending message
            retryMessage(message)
        }
        binding.chatRecyclerView.apply {
            layoutManager = LinearLayoutManager(this@MainActivity).apply {
                stackFromEnd = true
            }
            adapter = chatAdapter
        }
    }

    private fun setupClickListeners() {
        binding.settingsButton.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }

        binding.kioskExitButton.setOnClickListener {
            exitKioskMode()
        }
    }

    private fun enterKioskMode() {
        kioskManager.enterKioskMode {
            binding.header.visibility = View.VISIBLE
            binding.inputBar.visibility = View.VISIBLE
            binding.kioskExitButton.visibility = View.GONE
        }
        binding.header.visibility = View.GONE
        binding.kioskExitButton.visibility = View.VISIBLE
    }

    private fun exitKioskMode() {
        kioskManager.exitKioskMode()
        binding.header.visibility = View.VISIBLE
        binding.inputBar.visibility = View.VISIBLE
        binding.kioskExitButton.visibility = View.GONE
    }

    private fun setupInputHandlers() {
        // Send button
        binding.sendButton.setOnClickListener {
            sendTextMessage()
        }

        // Enter key on keyboard
        binding.messageInput.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_SEND) {
                sendTextMessage()
                true
            } else false
        }

        // Voice button - tap to toggle recording
        binding.voiceButton.setOnClickListener {
            if (isRecording) {
                stopRecordingAndSend()
            } else {
                if (checkAudioPermission()) {
                    startRecording()
                } else {
                    requestAudioPermission()
                }
            }
        }
    }

    private fun sendTextMessage() {
        val text = binding.messageInput.text.toString().trim()
        if (text.isEmpty()) return

        binding.messageInput.setText("")

        // Create a pending message
        val message = ChatMessage(
            role = "user",
            content = text,
            timestamp = getCurrentTimestamp(),
            status = MessageStatus.PENDING
        )

        // Add to pending queue immediately (shows in UI)
        addPendingMessage(message)

        // Try to send
        sendMessageToServer(message)
    }

    private fun addPendingMessage(message: ChatMessage) {
        pendingMessages.value = pendingMessages.value + message
    }

    private fun updatePendingMessage(messageId: String, newStatus: MessageStatus) {
        pendingMessages.value = pendingMessages.value.map { msg ->
            if (msg.id == messageId) msg.copy(status = newStatus) else msg
        }
    }

    private fun removePendingMessage(messageId: String) {
        pendingMessages.value = pendingMessages.value.filter { it.id != messageId }
    }

    private fun retryMessage(message: ChatMessage) {
        if (message.status == MessageStatus.PENDING || message.status == MessageStatus.FAILED) {
            // Update status to pending
            updatePendingMessage(message.id, MessageStatus.PENDING)
            // Retry sending
            sendMessageToServer(message)
        }
    }

    private fun sendMessageToServer(message: ChatMessage) {
        lifecycleScope.launch {
            val isConnected = webSocketClient?.connectionStatus?.value == ConnectionStatus.CONNECTED

            if (!isConnected) {
                // Mark as failed immediately if offline
                updatePendingMessage(message.id, MessageStatus.FAILED)
                return@launch
            }

            try {
                val result = withContext(Dispatchers.IO) {
                    sendTextToServer(message.content)
                }
                if (result) {
                    // Success - remove from pending (server will broadcast it back)
                    removePendingMessage(message.id)
                } else {
                    // Failed
                    updatePendingMessage(message.id, MessageStatus.FAILED)
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error sending message", e)
                updatePendingMessage(message.id, MessageStatus.FAILED)
            }
        }
    }

    private fun retryAllPendingMessages() {
        val messages = pendingMessages.value.filter {
            it.status == MessageStatus.FAILED || it.status == MessageStatus.PENDING
        }
        messages.forEach { message ->
            updatePendingMessage(message.id, MessageStatus.PENDING)
            sendMessageToServer(message)
        }
    }

    private fun getCurrentTimestamp(): String {
        val sdf = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US)
        return sdf.format(Date())
    }

    private fun sendTextToServer(text: String): Boolean {
        val serverAddress = SettingsActivity.getServerAddress(this)
        val baseUrl = "http://${serverAddress.replace(":5567", ":5566")}"
        val url = "$baseUrl/api/message"

        val json = JSONObject().apply {
            put("text", text)
            put("response_mode", "text")
        }

        val request = Request.Builder()
            .url(url)
            .post(json.toString().toRequestBody("application/json".toMediaType()))
            .build()

        return try {
            val response = httpClient.newCall(request).execute()
            response.isSuccessful
        } catch (e: Exception) {
            Log.e(TAG, "Error sending text", e)
            false
        }
    }

    private fun checkAudioPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            this, Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
    }

    private fun requestAudioPermission() {
        ActivityCompat.requestPermissions(
            this,
            arrayOf(Manifest.permission.RECORD_AUDIO),
            PERMISSION_REQUEST_CODE
        )
    }

    private fun startRecording() {
        try {
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
            binding.voiceButton.setBackgroundResource(R.drawable.bg_circle_button_recording)
            Toast.makeText(this, "Recording...", Toast.LENGTH_SHORT).show()

        } catch (e: Exception) {
            Log.e(TAG, "Failed to start recording", e)
            Toast.makeText(this, "Failed to start recording", Toast.LENGTH_SHORT).show()
        }
    }

    private fun stopRecordingAndSend() {
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
        binding.voiceButton.setBackgroundResource(R.drawable.bg_circle_button)

        audioFile?.let { file ->
            if (file.exists() && file.length() > 0) {
                sendAudioToServer(file)
            }
        }
    }

    private fun sendAudioToServer(file: File) {
        lifecycleScope.launch {
            try {
                val result = withContext(Dispatchers.IO) {
                    val serverAddress = SettingsActivity.getServerAddress(this@MainActivity)
                    val baseUrl = "http://${serverAddress.replace(":5567", ":5566")}"
                    val url = "$baseUrl/transcribe"

                    val request = Request.Builder()
                        .url(url)
                        .header("X-Response-Mode", "text")
                        .post(file.asRequestBody("audio/mp4".toMediaType()))
                        .build()

                    val response = httpClient.newCall(request).execute()
                    response.isSuccessful
                }

                if (!result) {
                    Toast.makeText(this@MainActivity, "Failed to send audio", Toast.LENGTH_SHORT).show()
                }

                file.delete()
                audioFile = null

            } catch (e: Exception) {
                Log.e(TAG, "Error sending audio", e)
                Toast.makeText(this@MainActivity, "Error: ${e.message}", Toast.LENGTH_SHORT).show()
            }
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == PERMISSION_REQUEST_CODE) {
            if (grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                Toast.makeText(this, "Tap mic button to record", Toast.LENGTH_SHORT).show()
            } else {
                Toast.makeText(this, "Microphone permission denied", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun connectWebSocket() {
        val serverAddress = SettingsActivity.getServerAddress(this)
        webSocketClient = WebSocketClient(serverAddress)

        lifecycleScope.launch {
            webSocketClient?.connectionStatus?.collectLatest { status ->
                updateConnectionUI(status)
                updateCreatureForConnection(status)

                // Retry pending messages when reconnected (if enabled)
                if (status == ConnectionStatus.CONNECTED && SettingsActivity.isAutoRetryEnabled(this@MainActivity)) {
                    retryAllPendingMessages()
                }
            }
        }

        lifecycleScope.launch {
            webSocketClient?.claudeState?.collectLatest { state ->
                updateCreatureState(state.status)
                resetIdleTimeout()
            }
        }

        // Combine server messages with pending messages
        lifecycleScope.launch {
            combine(
                webSocketClient?.chatMessages ?: MutableStateFlow(emptyList()),
                pendingMessages
            ) { serverMessages, pending ->
                // Merge: server messages + pending messages (sorted by timestamp)
                (serverMessages + pending).sortedBy { it.timestamp }
            }.collectLatest { allMessages ->
                chatAdapter.submitList(allMessages.toList())
                if (allMessages.isNotEmpty()) {
                    binding.chatRecyclerView.smoothScrollToPosition(allMessages.size - 1)
                }
            }
        }

        // Collect prompt state
        lifecycleScope.launch {
            webSocketClient?.currentPrompt?.collectLatest { prompt ->
                updatePromptUI(prompt)
            }
        }

        // Collect context usage
        lifecycleScope.launch {
            webSocketClient?.contextUsage?.collectLatest { usage ->
                updateContextUI(usage)
            }
        }

        // Collect mood updates
        lifecycleScope.launch {
            webSocketClient?.moodUpdate?.collectLatest { mood ->
                binding.creatureView.setMood(mood.mood)
                binding.backgroundView.setTheme(mood.background)
            }
        }

        webSocketClient?.connect()
    }

    private fun updateContextUI(usage: ContextUsage) {
        if (usage.totalContext > 0) {
            binding.contextBar.visibility = View.VISIBLE
            binding.contextProgress.progress = usage.contextPercent.toInt().coerceIn(0, 100)
            binding.contextPercent.text = "${usage.contextPercent.toInt()}%"

            // Change color based on usage level
            val color = when {
                usage.contextPercent >= 80 -> ContextCompat.getColor(this, android.R.color.holo_red_light)
                usage.contextPercent >= 60 -> ContextCompat.getColor(this, android.R.color.holo_orange_light)
                else -> ContextCompat.getColor(this, android.R.color.holo_green_light)
            }

            val progressDrawable = binding.contextProgress.progressDrawable as? LayerDrawable
            val progressLayer = progressDrawable?.findDrawableByLayerId(android.R.id.progress)
            if (progressLayer is ClipDrawable) {
                (progressLayer.drawable as? GradientDrawable)?.setColor(color)
            }
        } else {
            binding.contextBar.visibility = View.GONE
        }
    }

    private fun updatePromptUI(prompt: ClaudePrompt?) {
        if (prompt == null) {
            binding.promptContainer.visibility = View.GONE
            return
        }

        binding.promptContainer.visibility = View.VISIBLE

        // Show title if present
        if (!prompt.title.isNullOrEmpty()) {
            binding.promptTitle.text = prompt.title
            binding.promptTitle.visibility = View.VISIBLE
        } else {
            binding.promptTitle.visibility = View.GONE
        }

        // Show context if present (e.g., bash command)
        if (!prompt.context.isNullOrEmpty()) {
            binding.promptContext.text = prompt.context
            binding.promptContext.visibility = View.VISIBLE
        } else {
            binding.promptContext.visibility = View.GONE
        }

        binding.promptQuestion.text = prompt.question

        // Clear existing options
        binding.promptOptions.removeAllViews()

        // Add option buttons
        for (option in prompt.options) {
            val optionView = layoutInflater.inflate(R.layout.item_prompt_option, binding.promptOptions, false)

            val numText = optionView.findViewById<TextView>(R.id.optionNum)
            val labelText = optionView.findViewById<TextView>(R.id.optionLabel)
            val descText = optionView.findViewById<TextView>(R.id.optionDescription)

            numText.text = option.num.toString()
            labelText.text = option.label
            if (option.description.isNotEmpty()) {
                descText.text = option.description
                descText.visibility = View.VISIBLE
            } else {
                descText.visibility = View.GONE
            }

            if (option.selected) {
                optionView.isSelected = true
            }

            optionView.setOnClickListener {
                respondToPrompt(option.num)
            }

            binding.promptOptions.addView(optionView)
        }

        // Scroll chat to bottom to show prompt
        binding.chatRecyclerView.post {
            val itemCount = chatAdapter.itemCount
            if (itemCount > 0) {
                binding.chatRecyclerView.smoothScrollToPosition(itemCount - 1)
            }
        }
    }

    private fun respondToPrompt(optionNum: Int) {
        val currentPromptValue = webSocketClient?.currentPrompt?.value ?: return

        lifecycleScope.launch {
            try {
                val result = withContext(Dispatchers.IO) {
                    val serverAddress = SettingsActivity.getServerAddress(this@MainActivity)
                    val baseUrl = "http://${serverAddress.replace(":5567", ":5566")}"

                    if (currentPromptValue.isPermission && currentPromptValue.requestId != null) {
                        // Permission request - use permission endpoint
                        val url = "$baseUrl/api/permission/respond"
                        val decision = if (optionNum == 1) "allow" else "deny"

                        val json = JSONObject().apply {
                            put("request_id", currentPromptValue.requestId)
                            put("decision", decision)
                            put("reason", "User ${decision}ed from mobile app")
                        }

                        val request = Request.Builder()
                            .url(url)
                            .post(json.toString().toRequestBody("application/json".toMediaType()))
                            .build()

                        val response = httpClient.newCall(request).execute()
                        response.isSuccessful
                    } else {
                        // Regular prompt - use prompt endpoint
                        val url = "$baseUrl/api/prompt/respond"

                        val json = JSONObject().apply {
                            put("option", optionNum)
                        }

                        val request = Request.Builder()
                            .url(url)
                            .post(json.toString().toRequestBody("application/json".toMediaType()))
                            .build()

                        val response = httpClient.newCall(request).execute()
                        response.isSuccessful
                    }
                }

                if (result) {
                    // Hide prompt (server will send update via WebSocket)
                    binding.promptContainer.visibility = View.GONE
                } else {
                    Toast.makeText(this@MainActivity, "Failed to send response", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error responding to prompt", e)
                Toast.makeText(this@MainActivity, "Error: ${e.message}", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun updateConnectionUI(status: ConnectionStatus) {
        val (color, text) = when (status) {
            ConnectionStatus.CONNECTED -> Pair(R.color.status_connected, R.string.connected)
            ConnectionStatus.CONNECTING -> Pair(R.color.status_connecting, R.string.connecting)
            ConnectionStatus.DISCONNECTED -> Pair(R.color.status_disconnected, R.string.disconnected)
        }

        binding.connectionStatus.setText(text)
        (binding.connectionIndicator.background as? GradientDrawable)?.setColor(
            ContextCompat.getColor(this, color)
        )
    }

    private fun updateCreatureForConnection(status: ConnectionStatus) {
        if (status == ConnectionStatus.DISCONNECTED) {
            binding.creatureView.setState(CreatureState.OFFLINE)
        }
    }

    private fun updateCreatureState(status: String) {
        val creatureState = when (status) {
            "listening" -> CreatureState.LISTENING
            "thinking" -> CreatureState.THINKING
            "speaking" -> CreatureState.SPEAKING
            else -> CreatureState.IDLE
        }
        binding.creatureView.setState(creatureState)
    }

    private fun resetIdleTimeout() {
        idleTimeout = System.currentTimeMillis() + 120_000
        binding.creatureView.postDelayed({
            checkForSleep()
        }, 120_000)
    }

    private fun checkForSleep() {
        if (System.currentTimeMillis() >= idleTimeout) {
            val currentStatus = webSocketClient?.claudeState?.value?.status
            if (currentStatus == "idle") {
                binding.creatureView.setState(CreatureState.SLEEPING)
            }
        }
    }

    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        if (ev.action == MotionEvent.ACTION_DOWN) {
            val state = webSocketClient?.claudeState?.value?.status
            if (state == "idle") {
                binding.creatureView.setState(CreatureState.IDLE)
                resetIdleTimeout()
            }
            kioskManager.handleTap(ev.x, ev.y)
        }
        return super.dispatchTouchEvent(ev)
    }

    override fun onResume() {
        super.onResume()
        webSocketClient?.let {
            if (it.connectionStatus.value == ConnectionStatus.DISCONNECTED) {
                it.connect()
            }
        } ?: connectWebSocket()

        if (SettingsActivity.isKioskModeEnabled(this) && !kioskManager.isInKioskMode()) {
            enterKioskMode()
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        mediaRecorder?.release()
        webSocketClient?.destroy()
        httpClient.dispatcher.executorService.shutdown()
    }
}
