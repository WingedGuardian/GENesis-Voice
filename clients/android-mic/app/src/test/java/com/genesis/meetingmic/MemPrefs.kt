package com.genesis.meetingmic

import android.content.SharedPreferences

/** Just enough SharedPreferences for the endpoint registry, held in memory. */
class MemPrefs : SharedPreferences {
    val m = HashMap<String, Any?>()

    override fun getAll(): MutableMap<String, *> = HashMap(m)
    override fun getString(key: String, defValue: String?) = m[key] as String? ?: defValue
    override fun getStringSet(key: String, defValues: MutableSet<String>?) = defValues
    override fun getInt(key: String, defValue: Int) = m[key] as Int? ?: defValue
    override fun getLong(key: String, defValue: Long) = m[key] as Long? ?: defValue
    override fun getFloat(key: String, defValue: Float) = m[key] as Float? ?: defValue
    override fun getBoolean(key: String, defValue: Boolean) = m[key] as Boolean? ?: defValue
    override fun contains(key: String) = key in m
    override fun registerOnSharedPreferenceChangeListener(l: SharedPreferences.OnSharedPreferenceChangeListener?) {}
    override fun unregisterOnSharedPreferenceChangeListener(l: SharedPreferences.OnSharedPreferenceChangeListener?) {}

    override fun edit(): SharedPreferences.Editor = object : SharedPreferences.Editor {
        val puts = HashMap<String, Any?>()
        val removes = HashSet<String>()
        override fun putString(key: String, value: String?) = apply { puts[key] = value }
        override fun putStringSet(key: String, values: MutableSet<String>?) = apply { puts[key] = values }
        override fun putInt(key: String, value: Int) = apply { puts[key] = value }
        override fun putLong(key: String, value: Long) = apply { puts[key] = value }
        override fun putFloat(key: String, value: Float) = apply { puts[key] = value }
        override fun putBoolean(key: String, value: Boolean) = apply { puts[key] = value }
        override fun remove(key: String) = apply { removes += key }
        override fun clear() = apply { removes += m.keys }
        override fun commit(): Boolean {
            removes.forEach { m.remove(it) }
            m.putAll(puts)
            return true
        }
        override fun apply() {
            commit()
        }
    }
}
