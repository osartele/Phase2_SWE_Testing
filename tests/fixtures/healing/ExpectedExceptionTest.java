import org.junit.Test;

public class ExpectedExceptionTest {
    @Test(expected = IllegalArgumentException.class)
    public void shouldThrowInvalidArg() {
        throw new IllegalArgumentException("bad");
    }
}
