package com.claudewatch.companion.chat

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.claudewatch.companion.R
import com.claudewatch.companion.network.ChatMessage
import com.claudewatch.companion.network.MessageStatus

class ChatAdapter(
    private val onRetryClick: ((ChatMessage) -> Unit)? = null
) : ListAdapter<ChatMessage, ChatAdapter.MessageViewHolder>(MessageDiffCallback()) {

    companion object {
        private const val VIEW_TYPE_USER = 0
        private const val VIEW_TYPE_CLAUDE = 1
    }

    override fun getItemViewType(position: Int): Int {
        return if (getItem(position).role == "user") VIEW_TYPE_USER else VIEW_TYPE_CLAUDE
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): MessageViewHolder {
        val layoutId = if (viewType == VIEW_TYPE_USER) {
            R.layout.item_chat_user
        } else {
            R.layout.item_chat_claude
        }
        val view = LayoutInflater.from(parent.context).inflate(layoutId, parent, false)
        return MessageViewHolder(view)
    }

    override fun onBindViewHolder(holder: MessageViewHolder, position: Int) {
        val message = getItem(position)
        holder.bind(message)

        // Set click listener for failed messages
        if (message.status == MessageStatus.FAILED || message.status == MessageStatus.PENDING) {
            holder.itemView.setOnClickListener {
                onRetryClick?.invoke(message)
            }
        } else {
            holder.itemView.setOnClickListener(null)
            holder.itemView.isClickable = false
        }
    }

    class MessageViewHolder(itemView: View) : RecyclerView.ViewHolder(itemView) {
        private val messageText: TextView = itemView.findViewById(R.id.messageText)
        private val statusIndicator: TextView? = itemView.findViewById(R.id.statusIndicator)

        fun bind(message: ChatMessage) {
            messageText.text = message.content

            // Apply visual styling based on status
            when (message.status) {
                MessageStatus.SENT -> {
                    messageText.alpha = 1.0f
                    statusIndicator?.visibility = View.GONE
                }
                MessageStatus.PENDING -> {
                    messageText.alpha = 0.5f
                    statusIndicator?.visibility = View.VISIBLE
                    statusIndicator?.text = "Sending..."
                }
                MessageStatus.FAILED -> {
                    messageText.alpha = 0.5f
                    statusIndicator?.visibility = View.VISIBLE
                    statusIndicator?.text = "Tap to retry"
                }
            }
        }
    }

    class MessageDiffCallback : DiffUtil.ItemCallback<ChatMessage>() {
        override fun areItemsTheSame(oldItem: ChatMessage, newItem: ChatMessage): Boolean {
            return oldItem.id == newItem.id
        }

        override fun areContentsTheSame(oldItem: ChatMessage, newItem: ChatMessage): Boolean {
            return oldItem == newItem
        }
    }
}
