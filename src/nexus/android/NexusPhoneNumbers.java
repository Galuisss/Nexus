import android.os.IBinder;
import android.telephony.SubscriptionInfo;
import android.telephony.SubscriptionManager;

import java.lang.reflect.Method;
import java.util.Arrays;
import java.util.List;

import org.json.JSONArray;
import org.json.JSONObject;

/** Reads active SIM phone numbers via adb. Rebuild with scripts/build-phone-numbers.sh. */
public final class NexusPhoneNumbers {
    public static void main(String[] args) throws Exception {
        Object binder = Class.forName("android.os.ServiceManager")
                .getMethod("getService", String.class).invoke(null, "isub");
        Class<?> serviceClass = Class.forName("com.android.internal.telephony.ISub");
        Object service = Class.forName("com.android.internal.telephony.ISub$Stub")
                .getMethod("asInterface", IBinder.class).invoke(null, binder);
        Method activeSubscriptions = Arrays.stream(serviceClass.getMethods())
                .filter(method -> method.getName().equals("getActiveSubscriptionInfoList"))
                .findFirst().get();
        // The optional third argument selects subscriptions across user profiles.
        Object[] listArgs = activeSubscriptions.getParameterCount() == 3
                ? new Object[]{"com.android.shell", null, false}
                : new Object[]{"com.android.shell", null};
        List<?> subscriptions = (List<?>) activeSubscriptions.invoke(service, listArgs);
        Method getNumber = serviceClass.getMethod("getPhoneNumber", int.class, int.class,
                String.class, String.class);
        JSONArray result = new JSONArray();
        if (subscriptions != null) {
            for (Object subscription : subscriptions) {
                SubscriptionInfo info = (SubscriptionInfo) subscription;
                String number = "";
                for (int source : new int[]{SubscriptionManager.PHONE_NUMBER_SOURCE_CARRIER,
                        SubscriptionManager.PHONE_NUMBER_SOURCE_UICC,
                        SubscriptionManager.PHONE_NUMBER_SOURCE_IMS}) {
                    number = (String) getNumber.invoke(service, info.getSubscriptionId(), source,
                            "com.android.shell", null);
                    if (!number.isEmpty()) break;
                }
                result.put(new JSONObject()
                        .put("subscriptionId", info.getSubscriptionId())
                        .put("slotIndex", info.getSimSlotIndex())
                        .put("carrierName", info.getCarrierName())
                        .put("number", number.isEmpty() ? JSONObject.NULL : number));
            }
        }
        System.out.println(new JSONObject().put("subscriptions", result));
    }
}
