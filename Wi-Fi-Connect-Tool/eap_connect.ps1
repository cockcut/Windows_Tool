param(
    [string]$SSID = "",
    [string]$Username = "",
    [string]$Password = "",
    [string]$IsWpa3 = "0",
    [string]$Bssid = ""
)

$IsWpa3Bool = ($IsWpa3 -eq "1" -or $IsWpa3 -eq "true")

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public class WlanHelper {
    [DllImport("wlanapi.dll", SetLastError = true)]
    public static extern uint WlanOpenHandle(uint dwClientVersion, IntPtr pReserved, out uint pdwNegotiatedVersion, ref IntPtr phClientHandle);

    [DllImport("wlanapi.dll", SetLastError = true)]
    public static extern uint WlanCloseHandle(IntPtr hClientHandle, IntPtr pReserved);

    [DllImport("wlanapi.dll", SetLastError = true)]
    public static extern uint WlanEnumInterfaces(IntPtr hClientHandle, IntPtr pReserved, ref IntPtr ppInterfaceList);

    [DllImport("wlanapi.dll", SetLastError = true)]
    public static extern uint WlanScan(IntPtr hClientHandle, ref Guid pInterfaceGuid, IntPtr pDot11Ssid, IntPtr pIeData, IntPtr pReserved);

    [DllImport("wlanapi.dll", SetLastError = true)]
    public static extern uint WlanSetProfileEapXmlUserData(
        IntPtr hClientHandle,
        ref Guid pInterfaceGuid,
        [MarshalAs(UnmanagedType.LPWStr)] string strProfileName,
        uint dwFlags,
        [MarshalAs(UnmanagedType.LPWStr)] string strXmlUserData,
        IntPtr pReserved
    );

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct WLAN_CONNECTION_PARAMETERS {
        public uint wlanConnectionMode;
        [MarshalAs(UnmanagedType.LPWStr)]
        public string strProfile;
        public IntPtr pDot11Ssid;
        public IntPtr pDesiredBssidList;
        public uint dot11BssType;
        public uint dwFlags;
    }

    [DllImport("wlanapi.dll", SetLastError = true)]
    public static extern uint WlanConnect(
        IntPtr hClientHandle,
        ref Guid pInterfaceGuid,
        ref WLAN_CONNECTION_PARAMETERS pConnectionParameters,
        IntPtr pReserved
    );

    [DllImport("wlanapi.dll", SetLastError = true)]
    public static extern void WlanFreeMemory(IntPtr pMemory);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct WLAN_INTERFACE_INFO {
        public Guid interfaceGuid;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)]
        public string strInterfaceDescription;
        public uint isState;
    }

    public static string SetUserDataAndConnect(string profileName, string username, string password, string bssidParam) {
        StringBuilder log = new StringBuilder();
        IntPtr clientHandle = IntPtr.Zero;
        uint negotiatedVersion = 0;
        
        uint result = WlanOpenHandle(2, IntPtr.Zero, out negotiatedVersion, ref clientHandle);
        log.AppendLine("1. WlanOpenHandle: " + result);
        if (result != 0) return log.ToString();

        try {
            IntPtr ppInterfaceList = IntPtr.Zero;
            result = WlanEnumInterfaces(clientHandle, IntPtr.Zero, ref ppInterfaceList);
            log.AppendLine("2. WlanEnumInterfaces: " + result);
            if (result != 0) return log.ToString();

            try {
                uint dwNumberOfItems = (uint)Marshal.ReadInt32(ppInterfaceList, 0);
                if (dwNumberOfItems == 0) return log.ToString();

                IntPtr pItem = new IntPtr(ppInterfaceList.ToInt64() + 8);
                WLAN_INTERFACE_INFO info = (WLAN_INTERFACE_INFO)Marshal.PtrToStructure(pItem, typeof(WLAN_INTERFACE_INFO));
                Guid interfaceGuid = info.interfaceGuid;

                log.AppendLine("   Target NIC: " + info.strInterfaceDescription);

                string eapUserDataXml = "<?xml version=\"1.0\" encoding=\"utf-8\"?>" +
"<EapHostUserCredentials xmlns=\"http://www.microsoft.com/provisioning/EapHostUserCredentials\">" +
"  <EapMethod>" +
"    <Type xmlns=\"http://www.microsoft.com/provisioning/EapCommon\">25</Type>" +
"    <VendorId xmlns=\"http://www.microsoft.com/provisioning/EapCommon\">0</VendorId>" +
"    <VendorType xmlns=\"http://www.microsoft.com/provisioning/EapCommon\">0</VendorType>" +
"    <AuthorId xmlns=\"http://www.microsoft.com/provisioning/EapCommon\">0</AuthorId>" +
"  </EapMethod>" +
"  <Credentials>" +
"    <Eap xmlns=\"http://www.microsoft.com/provisioning/BaseEapUserPropertiesV1\">" +
"      <Type>25</Type>" +
"      <EapType xmlns=\"http://www.microsoft.com/provisioning/MsPeapUserPropertiesV1\">" +
"        <RoutingIdentity></RoutingIdentity>" +
"        <Eap xmlns=\"http://www.microsoft.com/provisioning/BaseEapUserPropertiesV1\">" +
"          <Type>26</Type>" +
"          <EapType xmlns=\"http://www.microsoft.com/provisioning/MsChapV2UserPropertiesV1\">" +
"            <Username>" + username + "</Username>" +
"            <Password>" + password + "</Password>" +
"            <LogonDomain></LogonDomain>" +
"          </EapType>" +
"        </Eap>" +
"      </EapType>" +
"    </Eap>" +
"  </Credentials>" +
"</EapHostUserCredentials>";

                uint setRes = WlanSetProfileEapXmlUserData(clientHandle, ref interfaceGuid, profileName, 1, eapUserDataXml, IntPtr.Zero);
                log.AppendLine("3. Credential Injection: " + setRes);

                if (setRes == 0) {
                    WlanScan(clientHandle, ref interfaceGuid, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
                    System.Threading.Thread.Sleep(1500);

                    WLAN_CONNECTION_PARAMETERS connParams = new WLAN_CONNECTION_PARAMETERS();
                    connParams.wlanConnectionMode = 0;
                    connParams.strProfile = profileName;
                    connParams.pDot11Ssid = IntPtr.Zero;
                    connParams.pDesiredBssidList = IntPtr.Zero;
                    connParams.dot11BssType = 1;
                    connParams.dwFlags = 0;

                    uint connRes = WlanConnect(clientHandle, ref interfaceGuid, ref connParams, IntPtr.Zero);
                    log.AppendLine("4. Native WlanConnect: " + connRes);
                }
            } finally {
                WlanFreeMemory(ppInterfaceList);
            }
        } finally {
            WlanCloseHandle(clientHandle, IntPtr.Zero);
        }

        return log.ToString();
    }
}
"@

function ConvertTo-Hex {
    param ([string]$InputString)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($InputString)
    return ($bytes | ForEach-Object { $_.ToString("X2") }) -join ""
}

function Get-ExactWlanXml {
    param ([string]$Ssid, [bool]$IsWpa3)
    $hexSsid = ConvertTo-Hex -InputString $Ssid
    $authType = if ($IsWpa3) { "WPA3ENT" } else { "WPA2" }

    return "<?xml version=""1.0""?>`n" +
"<WLANProfile xmlns=""http://www.microsoft.com/networking/WLAN/profile/v1"">`n" +
"	<name>$Ssid</name>`n" +
"	<SSIDConfig>`n" +
"		<SSID>`n" +
"			<hex>$hexSsid</hex>`n" +
"			<name>$Ssid</name>`n" +
"		</SSID>`n" +
"		<nonBroadcast>true</nonBroadcast>`n" +
"	</SSIDConfig>`n" +
"	<connectionType>ESS</connectionType>`n" +
"	<connectionMode>auto</connectionMode>`n" +
"	<autoSwitch>false</autoSwitch>`n" +
"	<MSM>`n" +
"		<security>`n" +
"			<authEncryption>`n" +
"				<authentication>$authType</authentication>`n" +
"				<encryption>AES</encryption>`n" +
"				<useOneX>true</useOneX>`n" +
"			</authEncryption>`n" +
"			<PMKCacheMode>enabled</PMKCacheMode>`n" +
"			<PMKCacheTTL>720</PMKCacheTTL>`n" +
"			<PMKCacheSize>128</PMKCacheSize>`n" +
"			<preAuthMode>disabled</preAuthMode>`n" +
"			<OneX xmlns=""http://www.microsoft.com/networking/OneX/v1"">`n" +
"				<cacheUserData>true</cacheUserData>`n" +
"				<authMode>user</authMode>`n" +
"				<EAPConfig><EapHostConfig xmlns=""http://www.microsoft.com/provisioning/EapHostConfig""><EapMethod><Type xmlns=""http://www.microsoft.com/provisioning/EapCommon"">25</Type><VendorId xmlns=""http://www.microsoft.com/provisioning/EapCommon"">0</VendorId><VendorType xmlns=""http://www.microsoft.com/provisioning/EapCommon"">0</VendorType><AuthorId xmlns=""http://www.microsoft.com/provisioning/EapCommon"">0</AuthorId></EapMethod><Config xmlns=""http://www.microsoft.com/provisioning/EapHostConfig""><Eap xmlns=""http://www.microsoft.com/provisioning/BaseEapConnectionPropertiesV1""><Type>25</Type><EapType xmlns=""http://www.microsoft.com/provisioning/MsPeapConnectionPropertiesV1""><ServerValidation><DisableUserPromptForServerValidation>true</DisableUserPromptForServerValidation><ServerNames></ServerNames></ServerValidation><FastReconnect>true</FastReconnect><InnerEapOptional>false</InnerEapOptional><Eap xmlns=""http://www.microsoft.com/provisioning/BaseEapConnectionPropertiesV1""><Type>26</Type><EapType xmlns=""http://www.microsoft.com/provisioning/MsChapV2ConnectionPropertiesV1""><UseWinLogonCredentials>false</UseWinLogonCredentials></EapType></Eap><EnableQuarantineChecks>false</EnableQuarantineChecks><RequireCryptoBinding>false</RequireCryptoBinding><PeapExtensions><PerformServerValidation xmlns=""http://www.microsoft.com/provisioning/MsPeapConnectionPropertiesV2"">false</PerformServerValidation><AcceptServerName xmlns=""http://www.microsoft.com/provisioning/MsPeapConnectionPropertiesV2"">false</AcceptServerName><IdentityPrivacy xmlns=""http://www.microsoft.com/provisioning/MsPeapConnectionPropertiesV2""><EnableIdentityPrivacy>false</EnableIdentityPrivacy><AnonymousUserName>anonymous</AnonymousUserName></IdentityPrivacy></PeapExtensions></EapType></Eap></Config></EapHostConfig></EAPConfig>`n" +
"			</OneX>`n" +
"		</security>`n" +
"	</MSM>`n" +
"</WLANProfile>"
}

# ===== Python에서 호출되는 백그라운드 로직 =====
$xmlContent = Get-ExactWlanXml -Ssid $SSID -IsWpa3 $IsWpa3Bool
$tempXmlPath = Join-Path $env:TEMP "temp_wifi_profile.xml"
[System.IO.File]::WriteAllText($tempXmlPath, $xmlContent, [System.Text.Encoding]::UTF8)

Start-Process "netsh.exe" -ArgumentList "wlan delete profile name=`"$SSID`"" -NoNewWindow -Wait
Start-Sleep -Milliseconds 300

$addProc = Start-Process "netsh.exe" -ArgumentList "wlan add profile filename=`"$tempXmlPath`" user=all" -NoNewWindow -PassThru -Wait
if (Test-Path $tempXmlPath) { Remove-Item $tempXmlPath -Force }

if ($addProc.ExitCode -ne 0) {
    Write-Output "FAIL|프로필 임포트 실패 (Exit Code: $($addProc.ExitCode))"
    exit 1
}

Start-Sleep -Milliseconds 800

$debugLog = [WlanHelper]::SetUserDataAndConnect($SSID, $Username, $Password, $Bssid)
Start-Sleep -Milliseconds 500

if ($Bssid -ne "") {
    Start-Process "netsh.exe" -ArgumentList "wlan connect name=`"$SSID`" ssid=`"$SSID`" bssid=`"$Bssid`"" -NoNewWindow -Wait
} else {
    Start-Process "netsh.exe" -ArgumentList "wlan connect name=`"$SSID`" ssid=`"$SSID`"" -NoNewWindow -Wait
}

if ($debugLog -match "4\. Native WlanConnect: 0") {
    Write-Output "SUCCESS|$debugLog"
} else {
    Write-Output "FAIL|$debugLog"
}