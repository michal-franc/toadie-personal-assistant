# Add project specific ProGuard rules here.

# OkHttp
-dontwarn okhttp3.**
-dontwarn okio.**
-keepnames class okhttp3.internal.publicsuffix.PublicSuffixDatabase

# Keep our app classes
-keep class com.claudewatch.app.** { *; }
