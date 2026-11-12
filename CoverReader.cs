// Reads a WinRT stream into base64. Windows PowerShell 5.1 can't cast WinRT streams itself, compiled C# can.
// AsStreamForRead copies until the end, so it works even when a player reports Size = 0 (Yandex Music does).
using System;
using System.IO;
using Windows.Storage.Streams;

public static class CoverReader
{
    public static string LastError = "";

    public static string Read(object stream)
    {
        try
        {
            using (var ns = ((IInputStream)stream).AsStreamForRead())
            using (var ms = new MemoryStream())
            {
                ns.CopyTo(ms);
                return Convert.ToBase64String(ms.ToArray());
            }
        }
        catch (Exception e) { LastError = e.GetType().Name + ": " + e.Message; return ""; }
    }
}
